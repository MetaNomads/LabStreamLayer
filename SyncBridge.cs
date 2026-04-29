// SyncBridge.cs
// Attach to any persistent GameObject in your Unity scene.
//
// - Listens for "ping_NNN" from SLS, logs receipt time, echoes ACK back
//   so SLS can measure round-trip latency.
// - Call SyncBridge.SendPing() to trigger a ping from Unity.
//
// Output: <persistentDataPath>/unity_ping_log_TIMESTAMP.csv
// Columns: ping_id, received_utc_epoch_ns

using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

public class SyncBridge : MonoBehaviour
{
    [Header("Network")]
    public string bridgeIP = "255.255.255.255";
    public int    udpPort  = 12345;

    // SECURITY/INTEGRITY: latched on first DISCOVER/HELLO handshake so a
    // second LSL host (or any node on the lab subnet sending "ping_*") can't
    // contaminate this session's recording. Released after HOST_QUIET_SECONDS
    // of silence so a host re-roam (DHCP change, WiFi-to-ethernet swap) can
    // re-handshake without forcing a Quest force-quit.
    private string _lockedHostIp = null;
    private long   _lastHostMessageTicks = 0;
    private const  double HOST_QUIET_SECONDS = 30.0;

    private UdpClient    _recv;
    private UdpClient    _send;
    private Thread       _thread;
    private StreamWriter _log;
    private bool         _running;

    private readonly ConcurrentQueue<(string id, long ns)> _q
        = new ConcurrentQueue<(string, long)>();

    private static SyncBridge _inst;

    void Awake()
    {
        if (_inst != null && _inst != this) { Destroy(gameObject); return; }
        _inst = this;
        DontDestroyOnLoad(gameObject);
    }

    void Start()
    {
        string ts   = DateTime.UtcNow.ToString("yyyy-MM-dd_HH-mm-ss");
        string path = Path.Combine(
            Application.persistentDataPath,
            $"unity_ping_log_{ts}.csv"
        );
        _log = new StreamWriter(path, false, Encoding.UTF8) { AutoFlush = true };
        _log.WriteLine("ping_id,received_utc_epoch_ns");
        Debug.Log($"[SyncBridge] Log -> {path}");

        _send = new UdpClient();
        _send.EnableBroadcast = true;

        _recv    = new UdpClient(udpPort);
        _running = true;
        _thread  = new Thread(Listen) { IsBackground = true };
        _thread.Start();
        Debug.Log($"[SyncBridge] Listening on port {udpPort}");
    }

    void Update()
    {
        while (_q.TryDequeue(out var e))
        {
            _log.WriteLine($"{e.id},{e.ns}");
            Debug.Log($"[SyncBridge] {e.id}  ns={e.ns}");
        }
    }

    void OnDestroy()
    {
        _running = false;
        _recv?.Close();
        _send?.Close();
        _log?.Close();
    }

    // Quest-specific: emit don/doff markers so the host-side syncLog knows
    // why there's a gap. Without this, taking the headset off mid-session
    // produces a silent gap and no explanation. Both Pause AND Focus paths
    // are covered because Quest builds vary in which one fires for don/doff.
    private void EmitHeadsetEvent(string label)
    {
        if (_lockedHostIp == null) return;
        try
        {
            byte[] msg = Encoding.UTF8.GetBytes(label);
            _send.Send(msg, msg.Length, _lockedHostIp, udpPort);
            Debug.Log($"[SyncBridge] {label} -> {_lockedHostIp}");
        }
        catch (Exception e) { Debug.LogWarning($"[SyncBridge] {label} send failed: {e.Message}"); }
    }

    void OnApplicationPause(bool paused)
    {
        EmitHeadsetEvent(paused ? "headset_doffed" : "headset_donned");
    }

    // Some Quest/MetaXR SDK builds signal don/doff via Focus rather than Pause —
    // particularly for short doffs under the proximity-sensor "soft pause" threshold.
    // Idempotent on the host side (it's just a log row), so dual-emission is safe.
    void OnApplicationFocus(bool hasFocus)
    {
        EmitHeadsetEvent(hasFocus ? "headset_donned" : "headset_doffed");
    }

    void OnApplicationQuit()
    {
        EmitHeadsetEvent("app_quitting");
    }

    // Call from game code to trigger a ping from Unity side:
    //   SyncBridge.SendPing();
    public static void SendPing()
    {
        if (_inst == null) { Debug.LogWarning("[SyncBridge] No instance."); return; }
        byte[] msg = Encoding.UTF8.GetBytes("PING");
        try { _inst._send.Send(msg, msg.Length, _inst.bridgeIP, _inst.udpPort); }
        catch (Exception e) { Debug.LogWarning($"[SyncBridge] Send error: {e.Message}"); }
    }

    private void Listen()
    {
        var ep = new IPEndPoint(IPAddress.Any, 0);
        while (_running)
        {
            try
            {
                byte[] data = _recv.Receive(ref ep);
                string msg  = Encoding.UTF8.GetString(data).Trim();

                // Latch onto the first peer that sends us a recognised host
                // message, and reject pings from any other source thereafter.
                // Lock auto-releases after HOST_QUIET_SECONDS so a host re-roam
                // (DHCP change / WiFi-to-ethernet) can re-handshake.
                string srcIp = ep.Address.ToString();
                long nowTicks = DateTime.UtcNow.Ticks;
                if (_lockedHostIp != null && _lastHostMessageTicks > 0)
                {
                    double sinceLastSec = (nowTicks - _lastHostMessageTicks) / (double)TimeSpan.TicksPerSecond;
                    if (sinceLastSec > HOST_QUIET_SECONDS)
                    {
                        Debug.Log($"[SyncBridge] Lock released (host quiet {sinceLastSec:F0}s) — re-handshake allowed");
                        _lockedHostIp = null;
                    }
                }
                if (_lockedHostIp == null &&
                    (msg.StartsWith("DISCOVER") || msg.StartsWith("CONNECT") ||
                     msg.StartsWith("ping_")    || msg == "PING"))
                {
                    _lockedHostIp = srcIp;
                    _lastHostMessageTicks = nowTicks;
                    Debug.Log($"[SyncBridge] Locked to LSL host {_lockedHostIp}");
                }
                if (_lockedHostIp != null && srcIp != _lockedHostIp)
                {
                    Debug.LogWarning($"[SyncBridge] Dropping {msg.Substring(0, System.Math.Min(20, msg.Length))} from {srcIp} (not locked host {_lockedHostIp})");
                    continue;
                }
                if (_lockedHostIp != null && srcIp == _lockedHostIp)
                {
                    _lastHostMessageTicks = nowTicks;   // refresh quiet timer
                }

                if (msg.StartsWith("ping_") || msg.StartsWith("__calib"))
                {
                    // Record receipt time
                    long ns = ToUnixNs(DateTime.UtcNow);
                    if (msg.StartsWith("ping_")) _q.Enqueue((msg, ns));

                    // Echo ACK back to SLS so it can measure round-trip latency
                    // Wire format (must match unity.py:_handle expectations):
                    //   "ACK:<ping_id>:<utc_epoch_ns>"
                    // The trailing ns lets the host write a Unity row in syncLog.csv.
                    string ackStr = $"ACK:{msg}:{ns}";
                    byte[] ack = Encoding.UTF8.GetBytes(ackStr);
                    try { _send.Send(ack, ack.Length, ep.Address.ToString(), udpPort); }
                    catch { /* best effort */ }
                }
            }
            catch (SocketException) { break; }
            catch (Exception e) { Debug.LogWarning($"[SyncBridge] {e.Message}"); }
        }
    }

    private static long ToUnixNs(DateTime utc)
    {
        var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        return (utc - epoch).Ticks * 100L;
    }
}
