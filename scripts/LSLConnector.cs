// LSLConnector.cs
// Thin MonoBehaviour wrapper — attach to any GameObject for Inspector wiring.
// All actual logic lives in LSLService.cs (no scene dependency).

using UnityEngine;

namespace MetaFrame.Data
{
    public class LSLConnector : MonoBehaviour
    {
        /// <summary>
        /// Ask LSL to start recording.
        /// Wire to any UnityEvent or call directly from your scripts.
        /// </summary>
        public void RequestLSLRecordingStart() => LSLService.RequestRecordingStart();

        /// <summary>
        /// Ask LSL to stop recording.
        /// Wire to any UnityEvent or call directly from your scripts.
        /// </summary>
        public void RequestLSLRecordingStop() => LSLService.RequestRecordingStop();
    }
}
