using MissionPlanner.Controls;
using MissionPlanner.Plugin;
using MissionPlanner.Utilities;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace TalkToFlyMissionPlanner
{
    public class TalkToFlyPlugin : Plugin
    {
        private ToolStripButton _button;
        private TalkToFlyForm _form;

        public override string Name { get { return "Talk-to-Fly"; } }
        public override string Version { get { return "0.1.1"; } }
        public override string Author { get { return "Jack Morgan"; } }

        public override bool Init()
        {
            return true;
        }

        public override bool Loaded()
        {
            try
            {
                Host.MainForm.BeginInvoke((MethodInvoker)delegate
                {
                    _button = new ToolStripButton();
                    _button.Text = "Talk-to-Fly";
                    _button.DisplayStyle = ToolStripItemDisplayStyle.Text;
                    _button.Click += delegate { ShowForm(); };
                    Host.MainForm.MainMenu.Items.Add(_button);
                });
                return true;
            }
            catch
            {
                return false;
            }
        }

        public override bool Exit()
        {
            try
            {
                if (_form != null && !_form.IsDisposed)
                {
                    _form.Close();
                }

                if (_button != null)
                {
                    Host.MainForm.BeginInvoke((MethodInvoker)delegate
                    {
                        Host.MainForm.MainMenu.Items.Remove(_button);
                        _button.Dispose();
                    });
                }
            }
            catch
            {
            }
            return true;
        }

        private void ShowForm()
        {
            if (_form == null || _form.IsDisposed)
            {
                _form = new TalkToFlyForm(Host.MainForm);
            }

            if (_form.WindowState == FormWindowState.Minimized)
                _form.WindowState = FormWindowState.Normal;

            _form.Show(Host.MainForm);
            _form.BringToFront();
            _form.Focus();
        }
    }

    public class TalkToFlyForm : Form
    {
        private readonly Form _hostMainForm;
        private readonly object sync = new object();
        private readonly ToolTip tooltip = new ToolTip();

        private TextBox txtBaseUrl;
        private TextBox txtLaunchCommand;
        private MyButton btnStartBackend;
        private MyButton btnPing;
        private Label lblConnection;
        private Label lblMission;
        private Label lblVehicle;
        private TextBox txtTask;
        private MyButton btnSubmitTask;
        private TextBox txtClarification;
        private MyButton btnSendClarification;
        private MyButton btnApprove;
        private MyButton btnCancelPreview;
        private MyButton btnAbort;
        private MyButton btnRefresh;
        private TextBox txtPlan;
        private ListBox lstSteps;
        private TextBox txtEvents;
        private System.Windows.Forms.Timer pollTimer;
        private Process backendProcess;
        private bool busy = false;

        public TalkToFlyForm(Form hostMainForm)
        {
            _hostMainForm = hostMainForm;
            InitializeUi();
        }

        private void InitializeUi()
        {
            Text = "Talk-to-Fly";
            Width = 980;
            Height = 760;
            MinimumSize = new Size(860, 620);
            StartPosition = FormStartPosition.CenterParent;
            if (_hostMainForm != null)
            {
                Font = _hostMainForm.Font;
                Icon = _hostMainForm.Icon;
            }

            var root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(8);
            root.ColumnCount = 1;
            root.RowCount = 5;
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 104));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 92));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 74));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 58));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 42));
            Controls.Add(root);

            var connGroup = CreateGroupBox("Connection");
            var connPanel = new TableLayoutPanel();
            connPanel.Dock = DockStyle.Fill;
            connPanel.ColumnCount = 6;
            connPanel.RowCount = 2;
            connPanel.Padding = new Padding(6);
            for (int i = 0; i < 6; i++)
                connPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 16.66f));
            connPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            connPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50));

            connPanel.Controls.Add(CreateLabel("Bridge URL"), 0, 0);
            txtBaseUrl = CreateTextBox("http://127.0.0.1:8765");
            connPanel.Controls.Add(txtBaseUrl, 1, 0);
            connPanel.SetColumnSpan(txtBaseUrl, 2);

            connPanel.Controls.Add(CreateLabel("Backend launch command"), 3, 0);
            txtLaunchCommand = CreateTextBox("cd /home/jack/Capstone/Talk-to-Fly && source /home/jack/Capstone/ardupilot/venv/ardupilot/bin/activate && poetry run python -m talk_to_fly.bridge -s -k");
            connPanel.Controls.Add(txtLaunchCommand, 4, 0);
            connPanel.SetColumnSpan(txtLaunchCommand, 2);

            btnStartBackend = CreateButton("Start backend", delegate { StartBackend(); }, DockStyle.Fill);
            connPanel.Controls.Add(btnStartBackend, 0, 1);
            btnPing = CreateButton("Ping", delegate { BeginRequest("GET", "/ping", null, true); }, DockStyle.Fill);
            connPanel.Controls.Add(btnPing, 1, 1);
            btnRefresh = CreateButton("Refresh", delegate { BeginRequest("GET", "/status", null, true); }, DockStyle.Fill);
            connPanel.Controls.Add(btnRefresh, 2, 1);
            lblConnection = CreateStatusLabel("Not connected");
            connPanel.Controls.Add(lblConnection, 3, 1);
            connPanel.SetColumnSpan(lblConnection, 3);
            connGroup.Controls.Add(connPanel);
            root.Controls.Add(connGroup, 0, 0);

            var taskGroup = CreateGroupBox("Tasking");
            var taskPanel = new TableLayoutPanel();
            taskPanel.Dock = DockStyle.Fill;
            taskPanel.ColumnCount = 3;
            taskPanel.RowCount = 2;
            taskPanel.Padding = new Padding(6);
            taskPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 70));
            taskPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 15));
            taskPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 15));
            taskPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            taskPanel.RowStyles.Add(new RowStyle(SizeType.Percent, 50));

            txtTask = CreateTextBox(string.Empty);
            txtTask.Multiline = true;
            taskPanel.Controls.Add(txtTask, 0, 0);
            taskPanel.SetColumnSpan(txtTask, 2);
            btnSubmitTask = CreateButton("Submit task", delegate { SubmitTask(); }, DockStyle.Fill);
            taskPanel.Controls.Add(btnSubmitTask, 2, 0);

            txtClarification = CreateTextBox(string.Empty);
            taskPanel.Controls.Add(txtClarification, 0, 1);
            taskPanel.SetColumnSpan(txtClarification, 2);
            btnSendClarification = CreateButton("Send clarification", delegate { SubmitClarification(); }, DockStyle.Fill);
            taskPanel.Controls.Add(btnSendClarification, 2, 1);

            taskGroup.Controls.Add(taskPanel);
            root.Controls.Add(taskGroup, 0, 1);

            var actionGroup = CreateGroupBox("Actions and status");
            var actionPanel = new FlowLayoutPanel();
            actionPanel.Dock = DockStyle.Fill;
            actionPanel.Padding = new Padding(6);
            actionPanel.FlowDirection = FlowDirection.LeftToRight;
            actionPanel.WrapContents = true;

            btnApprove = CreateButton("Approve plan", delegate { BeginRequest("POST", "/approve", null, false); }, DockStyle.None, 128);
            actionPanel.Controls.Add(btnApprove);
            btnCancelPreview = CreateButton("Cancel preview", delegate { BeginRequest("POST", "/cancel", null, false); }, DockStyle.None, 128);
            actionPanel.Controls.Add(btnCancelPreview);
            btnAbort = CreateButton("Abort / land", delegate { BeginRequest("POST", "/abort", null, false); }, DockStyle.None, 128);
            actionPanel.Controls.Add(btnAbort);

            lblMission = CreateInlineLabel("Mission: none");
            actionPanel.Controls.Add(lblMission);
            lblVehicle = CreateInlineLabel("Vehicle: unknown");
            actionPanel.Controls.Add(lblVehicle);
            actionGroup.Controls.Add(actionPanel);
            root.Controls.Add(actionGroup, 0, 2);

            var splitTop = new SplitContainer();
            splitTop.Dock = DockStyle.Fill;
            splitTop.Orientation = Orientation.Vertical;
            splitTop.SplitterDistance = 560;
            splitTop.Panel1.Padding = new Padding(0, 0, 4, 4);
            splitTop.Panel2.Padding = new Padding(4, 0, 0, 4);

            var planGroup = CreateGroupBox("Plan preview");
            txtPlan = CreateTextBox(string.Empty);
            txtPlan.Multiline = true;
            txtPlan.ReadOnly = true;
            txtPlan.ScrollBars = ScrollBars.Both;
            txtPlan.WordWrap = false;
            txtPlan.Dock = DockStyle.Fill;
            planGroup.Controls.Add(txtPlan);
            splitTop.Panel1.Controls.Add(planGroup);

            var stepsGroup = CreateGroupBox("Plan steps");
            lstSteps = new ListBox();
            lstSteps.Dock = DockStyle.Fill;
            stepsGroup.Controls.Add(lstSteps);
            splitTop.Panel2.Controls.Add(stepsGroup);
            root.Controls.Add(splitTop, 0, 3);

            var eventsGroup = CreateGroupBox("Events");
            txtEvents = CreateTextBox(string.Empty);
            txtEvents.Multiline = true;
            txtEvents.ReadOnly = true;
            txtEvents.ScrollBars = ScrollBars.Vertical;
            txtEvents.Dock = DockStyle.Fill;
            eventsGroup.Controls.Add(txtEvents);
            root.Controls.Add(eventsGroup, 0, 4);

            ThemeManager.ApplyThemeTo(this);
            UpdateConnectionLabel("Not connected", "Enter the bridge URL and start or ping the backend.");

            pollTimer = new System.Windows.Forms.Timer();
            pollTimer.Interval = 1000;
            pollTimer.Tick += delegate { if (!busy) BeginRequest("GET", "/status", null, true); };
            pollTimer.Start();
        }

        private static GroupBox CreateGroupBox(string title)
        {
            return new GroupBox
            {
                Text = title,
                Dock = DockStyle.Fill,
                Padding = new Padding(8)
            };
        }

        private static Label CreateLabel(string text)
        {
            return new Label
            {
                Text = text,
                Dock = DockStyle.Fill,
                TextAlign = ContentAlignment.MiddleLeft,
                AutoEllipsis = true
            };
        }

        private static Label CreateStatusLabel(string text)
        {
            return new Label
            {
                Text = text,
                Dock = DockStyle.Fill,
                TextAlign = ContentAlignment.MiddleLeft,
                AutoEllipsis = true,
                Padding = new Padding(2, 0, 0, 0)
            };
        }

        private static Label CreateInlineLabel(string text)
        {
            return new Label
            {
                Text = text,
                AutoSize = true,
                Margin = new Padding(16, 9, 0, 0)
            };
        }

        private static TextBox CreateTextBox(string text)
        {
            return new TextBox
            {
                Dock = DockStyle.Fill,
                Text = text,
                Margin = new Padding(3)
            };
        }

        private static MyButton CreateButton(string text, EventHandler onClick, DockStyle dock, int width = 0)
        {
            var button = new MyButton();
            button.Text = text;
            button.Dock = dock;
            if (width > 0)
                button.Width = width;
            button.Click += onClick;
            return button;
        }

        private void UpdateConnectionLabel(string summary, string detail)
        {
            lblConnection.Text = summary;
            tooltip.SetToolTip(lblConnection, detail ?? summary);
        }

        private void StartBackend()
        {
            if (backendProcess != null && !backendProcess.HasExited)
            {
                MessageBox.Show("Backend is already running from this panel.", "Talk-to-Fly");
                return;
            }

            string command = txtLaunchCommand.Text.Trim();
            if (string.IsNullOrEmpty(command))
            {
                MessageBox.Show("Set a backend launch command first.", "Talk-to-Fly");
                return;
            }

            try
            {
                var psi = new ProcessStartInfo();
                if (Environment.OSVersion.Platform == PlatformID.Unix || Environment.OSVersion.Platform == PlatformID.MacOSX)
                {
                    psi.FileName = "/bin/bash";
                    psi.Arguments = "-lc \"" + command.Replace("\"", "\\\"") + "\"";
                }
                else
                {
                    psi.FileName = "cmd.exe";
                    psi.Arguments = "/C " + command;
                }
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = false;
                psi.RedirectStandardError = false;
                backendProcess = Process.Start(psi);
                UpdateConnectionLabel("Backend launch command started", command);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Failed to start backend: " + ex.Message, "Talk-to-Fly");
            }
        }

        private void SubmitTask()
        {
            var task = txtTask.Text.Trim();
            if (task.Length == 0)
            {
                MessageBox.Show("Enter a task first.", "Talk-to-Fly");
                return;
            }

            var data = new Dictionary<string, string>();
            data["Task"] = task;
            BeginRequest("POST", "/task", data, false);
        }

        private void SubmitClarification()
        {
            var answer = txtClarification.Text.Trim();
            if (answer.Length == 0)
            {
                MessageBox.Show("Enter a clarification answer first.", "Talk-to-Fly");
                return;
            }

            var data = new Dictionary<string, string>();
            data["Answer"] = answer;
            BeginRequest("POST", "/clarification", data, false);
        }

        private void BeginRequest(string method, string path, Dictionary<string, string> formData, bool silentErrors)
        {
            lock (sync)
            {
                busy = true;
            }

            var currentBaseUrl = txtBaseUrl.Text.Trim().TrimEnd('/');
            Task.Run(delegate
            {
                BridgeResponse response = null;
                Exception failure = null;
                try
                {
                    response = Request(currentBaseUrl, method, path, formData);
                }
                catch (Exception ex)
                {
                    failure = ex;
                }

                BeginInvoke((MethodInvoker)delegate
                {
                    lock (sync)
                    {
                        busy = false;
                    }

                    if (failure != null)
                    {
                        var details = DescribeFailure(currentBaseUrl, path, failure);
                        UpdateConnectionLabel(details.Summary, details.Detail);
                        if (!silentErrors)
                            MessageBox.Show(details.Detail, "Talk-to-Fly");
                        return;
                    }

                    if (response != null)
                    {
                        ApplySnapshot(response.Snapshot);
                        if (response.Ok)
                        {
                            UpdateConnectionLabel(BuildConnectedSummary(response.Snapshot), currentBaseUrl + path + " responded successfully.");
                        }
                        else
                        {
                            var error = response.Error ?? "Request failed";
                            UpdateConnectionLabel("Bridge connected — request failed", error);
                            if (!silentErrors)
                                MessageBox.Show(error, "Talk-to-Fly");
                        }
                    }
                });
            });
        }

        private static string BuildConnectedSummary(Snapshot snapshot)
        {
            if (snapshot == null || snapshot.Bridge == null)
                return "Bridge connected";

            if (!snapshot.Bridge.WorkerAlive)
                return "Bridge connected — backend worker stopped";

            if (!string.IsNullOrEmpty(snapshot.Bridge.LastError))
                return "Bridge connected — backend warning";

            return "Bridge connected";
        }

        private BridgeResponse Request(string baseUrl, string method, string path, Dictionary<string, string> formData)
        {
            var request = (HttpWebRequest)WebRequest.Create(baseUrl + path);
            request.Method = method;
            request.Accept = "application/json";
            request.Timeout = 10000;
            request.ReadWriteTimeout = 10000;

            if (formData != null && method == "POST")
            {
                string body = BuildFormBody(formData);
                byte[] payload = Encoding.UTF8.GetBytes(body);
                request.ContentType = "application/x-www-form-urlencoded";
                request.ContentLength = payload.Length;
                using (var stream = request.GetRequestStream())
                {
                    stream.Write(payload, 0, payload.Length);
                }
            }

            using (var response = (HttpWebResponse)request.GetResponse())
            using (var stream = response.GetResponseStream())
            {
                var serializer = new DataContractJsonSerializer(typeof(BridgeResponse));
                return (BridgeResponse)serializer.ReadObject(stream);
            }
        }

        private static string BuildFormBody(Dictionary<string, string> formData)
        {
            var builder = new StringBuilder();
            bool first = true;
            foreach (var kvp in formData)
            {
                if (!first)
                    builder.Append('&');
                first = false;
                builder.Append(Uri.EscapeDataString(kvp.Key));
                builder.Append('=');
                builder.Append(Uri.EscapeDataString(kvp.Value ?? string.Empty));
            }
            return builder.ToString();
        }

        private static FailureDetails DescribeFailure(string baseUrl, string path, Exception ex)
        {
            if (string.IsNullOrWhiteSpace(baseUrl))
            {
                return new FailureDetails(
                    "Bridge URL missing",
                    "Enter the bridge URL first, for example http://127.0.0.1:8765.");
            }

            if (ex is UriFormatException)
            {
                return new FailureDetails(
                    "Invalid bridge URL",
                    "The bridge URL is not valid: " + baseUrl + "\n\nUse a URL like http://127.0.0.1:8765.");
            }

            var webException = ex as WebException;
            if (webException != null)
            {
                if (webException.Status == WebExceptionStatus.Timeout)
                {
                    return new FailureDetails(
                        "Bridge timeout",
                        "Timed out waiting for the bridge at " + baseUrl + path + ".\n\nThe bridge may be starting, blocked, or not responding.");
                }

                if (webException.Status == WebExceptionStatus.NameResolutionFailure)
                {
                    return new FailureDetails(
                        "Bridge host not found",
                        "Could not resolve the bridge host in " + baseUrl + ".\n\nCheck the host name or use 127.0.0.1 for a local bridge.");
                }

                if (webException.Status == WebExceptionStatus.ConnectFailure)
                {
                    var socketException = FindSocketException(webException);
                    if (socketException != null && socketException.SocketErrorCode == SocketError.ConnectionRefused)
                    {
                        return new FailureDetails(
                            "Bridge not listening",
                            "Nothing is listening at " + baseUrl + ".\n\nStart the backend first, or check that the bridge port is correct.");
                    }

                    return new FailureDetails(
                        "Bridge connection failed",
                        "Could not connect to " + baseUrl + ".\n\nCheck that talk_to_fly.bridge is running and that the port matches the plugin setting.");
                }

                if (webException.Status == WebExceptionStatus.ProtocolError)
                {
                    var httpResponse = webException.Response as HttpWebResponse;
                    var statusText = httpResponse != null
                        ? ((int)httpResponse.StatusCode).ToString() + " " + httpResponse.StatusDescription
                        : "HTTP protocol error";
                    return new FailureDetails(
                        "Bridge HTTP error",
                        "The bridge responded with " + statusText + " for " + path + ".\n\nCheck the bridge logs for the request failure.");
                }
            }

            return new FailureDetails(
                "Bridge request failed",
                "Request to the bridge failed.\n\n" + ex.Message);
        }

        private static SocketException FindSocketException(Exception ex)
        {
            while (ex != null)
            {
                var socketException = ex as SocketException;
                if (socketException != null)
                    return socketException;
                ex = ex.InnerException;
            }
            return null;
        }

        private void ApplySnapshot(Snapshot snapshot)
        {
            if (snapshot == null)
                return;

            if (snapshot.Drone != null)
            {
                lblVehicle.Text = string.Format(
                    "Vehicle: mode={0}, armed={1}, alt={2:0.0} m, gs={3:0.00} m/s",
                    snapshot.Drone.FlightMode,
                    snapshot.Drone.Armed,
                    snapshot.Drone.PositionAlt,
                    snapshot.Drone.GroundspeedMps);
            }

            if (snapshot.Mission == null)
            {
                lblMission.Text = "Mission: none";
                txtPlan.Text = string.Empty;
                lstSteps.Items.Clear();
                btnApprove.Enabled = false;
                btnCancelPreview.Enabled = false;
                btnAbort.Enabled = false;
                return;
            }

            lblMission.Text = string.Format(
                "Mission: {0} ({1}) replans {2}/{3}",
                snapshot.Mission.MissionId,
                snapshot.Mission.Status,
                snapshot.Mission.ReplanCount,
                snapshot.Mission.MaxReplans);

            var planBuilder = new StringBuilder();
            planBuilder.AppendLine("Objective:");
            planBuilder.AppendLine(snapshot.Mission.Objective ?? string.Empty);
            planBuilder.AppendLine();
            planBuilder.AppendLine("Plan source: " + (snapshot.Mission.CurrentPlanSource ?? string.Empty));
            planBuilder.AppendLine("Review: " + (snapshot.Mission.ReviewSummary ?? string.Empty));
            if (!string.IsNullOrEmpty(snapshot.Mission.PendingQuestion))
            {
                planBuilder.AppendLine();
                planBuilder.AppendLine("Pending clarification:");
                planBuilder.AppendLine(snapshot.Mission.PendingQuestion);
            }
            if (!string.IsNullOrEmpty(snapshot.Mission.LastFailureReason))
            {
                planBuilder.AppendLine();
                planBuilder.AppendLine("Last failure:");
                planBuilder.AppendLine(snapshot.Mission.LastFailureReason);
            }
            planBuilder.AppendLine();
            planBuilder.AppendLine("MiniSpec:");
            planBuilder.AppendLine(snapshot.Mission.CurrentPlan ?? string.Empty);
            txtPlan.Text = planBuilder.ToString();

            lstSteps.BeginUpdate();
            lstSteps.Items.Clear();
            if (snapshot.Mission.CurrentPlanSteps != null)
            {
                for (int i = 0; i < snapshot.Mission.CurrentPlanSteps.Count; i++)
                {
                    string prefix = i == snapshot.Mission.NextStepIdx ? "-> " : "   ";
                    lstSteps.Items.Add(prefix + (i + 1).ToString("00") + ". " + snapshot.Mission.CurrentPlanSteps[i]);
                }
            }
            lstSteps.EndUpdate();

            string status = snapshot.Mission.Status ?? string.Empty;
            btnApprove.Enabled = status == "awaiting_approval";
            btnCancelPreview.Enabled = status == "awaiting_approval" || status == "awaiting_clarification";
            btnAbort.Enabled = status == "executing" || status == "replanning" || status == "awaiting_approval" || status == "awaiting_clarification";

            if (snapshot.Events != null)
            {
                var eventsBuilder = new StringBuilder();
                foreach (var evt in snapshot.Events)
                {
                    DateTime dt = DateTimeOffset.FromUnixTimeSeconds((long)evt.Timestamp).LocalDateTime;
                    eventsBuilder.Append('[').Append(dt.ToString("HH:mm:ss")).Append("] ");
                    eventsBuilder.Append(evt.Level).Append(": ").AppendLine(evt.Message);
                }
                txtEvents.Text = eventsBuilder.ToString();
                txtEvents.SelectionStart = txtEvents.TextLength;
                txtEvents.ScrollToCaret();
            }
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            if (pollTimer != null)
                pollTimer.Stop();
            base.OnFormClosed(e);
        }

        private sealed class FailureDetails
        {
            public FailureDetails(string summary, string detail)
            {
                Summary = summary;
                Detail = detail;
            }

            public string Summary { get; private set; }
            public string Detail { get; private set; }
        }
    }

    [DataContract]
    public class BridgeResponse
    {
        [DataMember]
        public bool Ok { get; set; }

        [DataMember]
        public string Error { get; set; }

        [DataMember]
        public Snapshot Snapshot { get; set; }
    }

    [DataContract]
    public class Snapshot
    {
        [DataMember]
        public BridgeInfo Bridge { get; set; }

        [DataMember]
        public MissionInfo Mission { get; set; }

        [DataMember]
        public DroneInfo Drone { get; set; }

        [DataMember]
        public ConversationInfo Conversation { get; set; }

        [DataMember]
        public List<EventInfo> Events { get; set; }
    }

    [DataContract]
    public class BridgeInfo
    {
        [DataMember]
        public bool ApprovalRequired { get; set; }

        [DataMember]
        public bool WorkerAlive { get; set; }

        [DataMember]
        public string LastError { get; set; }

        [DataMember]
        public string Connect { get; set; }

        [DataMember]
        public bool Simulation { get; set; }

        [DataMember]
        public bool Verbose { get; set; }

        [DataMember]
        public bool Confirm { get; set; }

        [DataMember]
        public double Timestamp { get; set; }
    }

    [DataContract]
    public class MissionInfo
    {
        [DataMember]
        public string MissionId { get; set; }

        [DataMember]
        public string UserTask { get; set; }

        [DataMember]
        public string Objective { get; set; }

        [DataMember]
        public string Status { get; set; }

        [DataMember]
        public string CurrentPlan { get; set; }

        [DataMember]
        public List<string> CurrentPlanSteps { get; set; }

        [DataMember]
        public string CurrentPlanSource { get; set; }

        [DataMember]
        public int NextStepIdx { get; set; }

        [DataMember]
        public List<string> CompletedSteps { get; set; }

        [DataMember]
        public string PendingQuestion { get; set; }

        [DataMember]
        public int ReplanCount { get; set; }

        [DataMember]
        public int MaxReplans { get; set; }

        [DataMember]
        public int LocalRecoveryCount { get; set; }

        [DataMember]
        public string ReviewSummary { get; set; }

        [DataMember]
        public string LastFailureReason { get; set; }

        [DataMember]
        public int ExecutionHistoryCount { get; set; }
    }

    [DataContract]
    public class DroneInfo
    {
        [DataMember]
        public string FlightMode { get; set; }

        [DataMember]
        public bool Armed { get; set; }

        [DataMember]
        public bool Simulation { get; set; }

        [DataMember]
        public double BatteryPercent { get; set; }

        [DataMember]
        public double HeadingDegrees { get; set; }

        [DataMember]
        public double GroundspeedMps { get; set; }

        [DataMember]
        public double PositionLat { get; set; }

        [DataMember]
        public double PositionLon { get; set; }

        [DataMember]
        public double PositionAlt { get; set; }
    }

    [DataContract]
    public class ConversationInfo
    {
        [DataMember]
        public bool PendingClarification { get; set; }

        [DataMember]
        public string PendingQuestion { get; set; }

        [DataMember]
        public int TurnCount { get; set; }
    }

    [DataContract]
    public class EventInfo
    {
        [DataMember]
        public int Seq { get; set; }

        [DataMember]
        public double Timestamp { get; set; }

        [DataMember]
        public string Level { get; set; }

        [DataMember]
        public string Message { get; set; }

        [DataMember]
        public string MissionId { get; set; }
    }
}
