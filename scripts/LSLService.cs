// LSLService.cs
// Place anywhere in your Assets folder — no scene setup needed.
//
// [InitializeOnLoad]               → static ctor runs on every domain reload (Editor)
// [RuntimeInitializeOnLoadMethod]  → runs when play mode starts, finds DataManager
//
// Connection state (IP, connected flag) is stored in SessionState so it
// survives domain reloads. After reload the connector sends RECONNECT to
// LSL so LSL updates its target without a full re-handshake.

using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

#if UNITY_EDITOR
using UnityEditor;
#endif

namespace MetaFrame.Data
{
#if UNITY_EDITOR
    [InitializeOnLoad]
#endif
    public static class LSLService
    {
        // ── Config ────────────────────────────────────────────────────────────
        public static int Port = 12345;

        // ── State ─────────────────────────────────────────────────────────────
        private static UdpClient      _recv;
        private static UdpClient      _send;
        private static readonly object _sendLock = new object();
        private static Thread          _listenThread;
        private static bool            _running     = false;
        private static bool            _initialized = false;

        private static string _lslIP     = null;
        private static bool   _connected = false;
        private static string _deviceName;

        private static double      _nextAnnounceTime = 0;
        private const  double      AnnounceInterval  = 3.0;

        private static volatile bool   _dataRequested = false;
        private static volatile string _pendingPingId  = null;

        // Deferred recording requests — fired as soon as connection is ready
        private static volatile bool _pendingRecordingStart = false;
        private static volatile bool _pendingRecordingStop  = false;

        // Set by LSLServiceTicker when play mode starts
        internal static DataManager DataManager;

#if UNITY_EDITOR
        private const string SESSION_IP   = "LSLService.lslIP";
        private const string SESSION_CONN = "LSLService.connected";
#endif

        // ── Editor bootstrap (runs on every domain reload) ────────────────────
#if UNITY_EDITOR
        static LSLService()
        {
            Init();
            EditorApplication.update += EditorTick;
            EditorApplication.playModeStateChanged += OnPlayModeChanged;
        }

        private static void OnPlayModeChanged(PlayModeStateChange state)
        {
            if (state == PlayModeStateChange.ExitingPlayMode)
            {
                RequestRecordingStop();
                DataManager = null;
            }
            else if (state == PlayModeStateChange.EnteredEditMode)
            {
                DataManager = null;
            }
        }

        private static void EditorTick()
        {
            if (!Application.isPlaying) Tick();
        }
#endif

        // ── Runtime bootstrap ─────────────────────────────────────────────────
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void RuntimeInit()
        {
            if (!_initialized) Init();
            var go = new GameObject("[LSLServiceTicker]")
                { hideFlags = HideFlags.HideAndDontSave };
            UnityEngine.Object.DontDestroyOnLoad(go);
            go.AddComponent<LSLServiceTicker>();
        }

        // ── Init ──────────────────────────────────────────────────────────────

        private static void Init()
        {
            if (_initialized) return;
            try
            {
                _deviceName = SystemInfo.deviceName ?? "Unity";

                _send = new UdpClient();
                _send.EnableBroadcast = true;

                _recv = new UdpClient();
                _recv.Client.SetSocketOption(
                    SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                _recv.Client.Bind(new IPEndPoint(IPAddress.Any, Port));

                _running = true;
                _listenThread = new Thread(ListenLoop)
                    { IsBackground = true, Name = "LSLService" };
                _listenThread.Start();

                _initialized = true;

                // Restore previous connection across domain reload
#if UNITY_EDITOR
                string ip   = SessionState.GetString(SESSION_IP, null);
                bool   conn = SessionState.GetBool(SESSION_CONN, false);
                if (conn && !string.IsNullOrEmpty(ip))
                {
                    _lslIP     = ip;
                    _connected = true;
                    SendTo($"RECONNECT,{_deviceName}", ip);
                    Debug.Log($"[LSLService] Restored — LSL at {ip}");
                    return;
                }
#endif
                Debug.Log($"[LSLService] Ready on :{Port} — broadcasting");
            }
            catch (Exception e)
            {
                Debug.LogError($"[LSLService] Init failed: {e.Message}");
                _running = false;
                try { _send?.Close(); } catch { }
                try { _recv?.Close(); } catch { }
            }
        }

        // ── Tick ──────────────────────────────────────────────────────────────

        internal static void Tick()
        {
            if (!_initialized) return;
            try
            {
                double now = GetTime();
                if (!_connected && now >= _nextAnnounceTime)
                {
                    SendTo($"HELLO,unity,{_deviceName}", "255.255.255.255");
                    _nextAnnounceTime = now + AnnounceInterval;
                }

                if (_connected)
                {
                    if (_pendingRecordingStart)
                    {
                        _pendingRecordingStart = false;
                        SendTo("RECORDING_STARTED", _lslIP);
                        Debug.Log("[LSLService] RECORDING_STARTED → LSL (deferred)");
                    }
                    if (_pendingRecordingStop)
                    {
                        _pendingRecordingStop = false;
                        SendTo("RECORDING_STOPPED", _lslIP);
                        Debug.Log("[LSLService] RECORDING_STOPPED → LSL (deferred)");
                    }
                }

                if (_dataRequested)
                {
                    _dataRequested = false;
                    if (Application.isPlaying) SendDataSnapshot();
                }

                string pid = _pendingPingId;
                if (pid != null)
                {
                    _pendingPingId = null;
                    long ns = ToUnixNs(DateTime.UtcNow);
                    SendTo($"ACK:{pid}:{ns}", _lslIP);
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning($"[LSLService] Tick: {e.Message}");
            }
        }

        // ── Listen loop ───────────────────────────────────────────────────────

        private static void ListenLoop()
        {
            var ep = new IPEndPoint(IPAddress.Any, 0);
            while (_running)
            {
                try
                {
                    if (!_recv.Client.Poll(500000, SelectMode.SelectRead))
                        continue;

                    byte[] data = _recv.Receive(ref ep);
                    string msg  = Encoding.UTF8.GetString(data).Trim();
                    string src  = ep.Address.ToString();

                    if (msg == "DISCOVER")
                    {
                        SendTo($"HELLO,unity,{_deviceName}", src);
                    }
                    else if (msg == "CONNECT")
                    {
                        _lslIP     = src;
                        _connected = true;
                        SendTo($"CONNECTED,{_deviceName}", src);
                        Debug.Log($"[LSLService] Connected — LSL at {src}");
#if UNITY_EDITOR
                        SessionState.SetString(SESSION_IP, src);
                        SessionState.SetBool(SESSION_CONN, true);
#endif
                        if (_pendingRecordingStart)
                        {
                            _pendingRecordingStart = false;
                            SendTo("RECORDING_STARTED", src);
                            Debug.Log("[LSLService] RECORDING_STARTED → LSL (on connect)");
                        }
                        if (_pendingRecordingStop)
                        {
                            _pendingRecordingStop = false;
                            SendTo("RECORDING_STOPPED", src);
                            Debug.Log("[LSLService] RECORDING_STOPPED → LSL (on connect)");
                        }
                    }
                    else if (msg == "DISCONNECT")
                    {
                        _connected = false;
                        _lslIP     = null;
#if UNITY_EDITOR
                        SessionState.SetBool(SESSION_CONN, false);
#endif
                        Debug.Log("[LSLService] Disconnected");
                    }
                    else if (msg.StartsWith("__calib_"))
                    {
                        SendTo("ACK:" + msg, src);
                    }
                    else if (msg == "REQUEST_DATA")
                    {
                        _dataRequested = true;
                    }
                    else if (msg.StartsWith("ping_"))
                    {
                        _pendingPingId = msg;
                    }
                }
                catch (ObjectDisposedException) { break; }
                catch (ThreadAbortException)    { Thread.ResetAbort(); break; }
                catch { }
            }
        }

        // ── Data snapshot ─────────────────────────────────────────────────────

        private static void SendDataSnapshot()
        {
            var dm = DataManager;
            if (dm == null || string.IsNullOrEmpty(_lslIP)) return;
            try
            {
                long ts = ToUnixNs(DateTime.UtcNow);
                var  sb = new System.Text.StringBuilder(256);
                sb.Append($"DATA,unity,{ts}");

                // Body — use public BodyData accessor (Body field is internal)
                try { var h = dm.BodyData?.Head;
                    if (h != null) { var r = h.rotation;
                        sb.Append($",headRot={r.x:F4},{r.y:F4},{r.z:F4},{r.w:F4}"); } } catch { }
                try { var rp = dm.BodyData?.RightHandPalm;
                    if (rp != null) { var r = rp.rotation;
                        sb.Append($",rightPalmRot={r.x:F4},{r.y:F4},{r.z:F4},{r.w:F4}"); } } catch { }
                try { var lp = dm.BodyData?.LeftHandPalm;
                    if (lp != null) { var r = lp.rotation;
                        sb.Append($",leftPalmRot={r.x:F4},{r.y:F4},{r.z:F4},{r.w:F4}"); } } catch { }

                // Gaze — use public GazeData accessor
                try { var g = dm.GazeData?.CenterGaze?.GazePoint;
                    if (g.HasValue) sb.Append($",gazePointX={g.Value.x:F4}"); } catch { }

                // FACS — use public FACSData accessor (FACS field is internal)
                try {
                    var facs = dm.FACSData;
                    if (facs != null)
                    {
                        var au1 = facs.AU1_InnerBrowRaiser;
                        // AU1 Inner Brow Raiser — aggregate (mean L+R)
                        if (au1.InnerBrowRaiserL.HasValue && au1.InnerBrowRaiserR.HasValue)
                            sb.Append($",au1={(au1.InnerBrowRaiserL.Value + au1.InnerBrowRaiserR.Value) * 0.5f:F4}");

                        // AU2 Outer Brow Raiser — aggregate
                        var au2 = facs.AU2_OuterBrowRaiser;
                        if (au2.OuterBrowRaiserL.HasValue && au2.OuterBrowRaiserR.HasValue)
                            sb.Append($",au2={(au2.OuterBrowRaiserL.Value + au2.OuterBrowRaiserR.Value) * 0.5f:F4}");

                        // AU4 Brow Lowerer — aggregate
                        var au4 = facs.AU4_BrowLowerer;
                        if (au4.BrowLowererL.HasValue && au4.BrowLowererR.HasValue)
                            sb.Append($",au4={(au4.BrowLowererL.Value + au4.BrowLowererR.Value) * 0.5f:F4}");

                        // AU43 Eyes Closed (blink) — aggregate
                        var au43 = facs.AU43_EyesClosed;
                        if (au43.EyesClosedL.HasValue && au43.EyesClosedR.HasValue)
                            sb.Append($",blink={(au43.EyesClosedL.Value + au43.EyesClosedR.Value) * 0.5f:F4}");
                    }
                } catch { }

                SendTo(sb.ToString(), _lslIP);
            }
            catch { }
        }

        // ── Public API ────────────────────────────────────────────────────────

        public static void RequestRecordingStart()
        {
            if (!_connected || string.IsNullOrEmpty(_lslIP))
            {
                _pendingRecordingStart = true;
                _pendingRecordingStop  = false;
                Debug.Log("[LSLService] RequestRecordingStart queued — waiting for connection");
                return;
            }
            _pendingRecordingStart = false;
            SendTo("RECORDING_STARTED", _lslIP);
            Debug.Log("[LSLService] RECORDING_STARTED → LSL");
        }

        public static void RequestRecordingStop()
        {
            _pendingRecordingStart = false;
            if (!_connected || string.IsNullOrEmpty(_lslIP))
            {
                _pendingRecordingStop = true;
                Debug.Log("[LSLService] RequestRecordingStop queued — waiting for connection");
                return;
            }
            _pendingRecordingStop = false;
            SendTo("RECORDING_STOPPED", _lslIP);
            Debug.Log("[LSLService] RECORDING_STOPPED → LSL");
        }

        // ── Helpers ───────────────────────────────────────────────────────────

        private static void SendTo(string msg, string ip)
        {
            if (string.IsNullOrEmpty(ip)) return;
            byte[] data = Encoding.UTF8.GetBytes(msg);
            lock (_sendLock)
            {
                try { _send.Send(data, data.Length, ip, Port); }
                catch { }
            }
        }

        private static double GetTime() =>
            (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;

        private static long ToUnixNs(DateTime utc) =>
            (utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).Ticks * 100L;
    }

    // ── Ticker: hidden GameObject, auto-created at runtime ────────────────────

    internal class LSLServiceTicker : MonoBehaviour
    {
        void Start()
        {
            // Find DataManager once scene is fully loaded
            LSLService.DataManager = FindObjectOfType<DataManager>();
            Debug.Log($"[LSLService] DataManager={(LSLService.DataManager != null ? "found" : "null")}");
        }

        void Update() => LSLService.Tick();

        void OnDestroy() => LSLService.DataManager = null;
    }
}
