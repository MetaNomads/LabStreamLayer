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

                if (msg.StartsWith("ping_"))
                {
                    // Record receipt time
                    long ns = ToUnixNs(DateTime.UtcNow);
                    _q.Enqueue((msg, ns));

                    // Echo ACK back to SLS so it can measure round-trip latency
                    // Format: "ACK:ping_001"
                    byte[] ack = Encoding.UTF8.GetBytes("ACK:" + msg);
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
