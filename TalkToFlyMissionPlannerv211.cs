using MissionPlanner.Plugin;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace TalkToFlyMissionPlanner
{
    public class TalkToFlyMissionPlannerPlugin : Plugin
    {
        private ToolStripButton _menuButton;
        private TalkToFlyMissionControlForm _form;

        public override string Name { get { return "Talk-to-Fly"; } }
        public override string Version { get { return "2.1.1"; } }
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
                    _menuButton = new ToolStripButton();
                    _menuButton.Text = "Talk-to-Fly";
                    _menuButton.DisplayStyle = ToolStripItemDisplayStyle.Text;
                    _menuButton.ToolTipText = "Open Talk-to-Fly Mission Control v2.1.1";
                    _menuButton.Click += delegate { ShowMissionControl(); };
                    Host.MainForm.MainMenu.Items.Add(_menuButton);
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

                if (_menuButton != null)
                {
                    Host.MainForm.BeginInvoke((MethodInvoker)delegate
                    {
                        Host.MainForm.MainMenu.Items.Remove(_menuButton);
                        _menuButton.Dispose();
                    });
                }
            }
            catch { }
            return true;
        }

        private void ShowMissionControl()
        {
            if (_form == null || _form.IsDisposed)
            {
                _form = new TalkToFlyMissionControlForm(Host.MainForm as Form);
            }

            _form.Show();
            _form.BringToFront();
        }
    }

    public sealed class TalkToFlyMissionControlForm : Form
    {
        private readonly Form _hostForm;
        private readonly System.Windows.Forms.Timer _refreshTimer;
        private readonly object _requestLock = new object();

        private TtfBridgeClient _client;
        private IDictionary<string, object> _lastSnapshot;
        private int _lastEventSeq = 0;
        private bool _refreshInProgress = false;
        private bool _bridgeOnline = false;
        private Process _bridgeProcess;
        private string _lastNaturalLanguageTask = null;

        private TextBox _bridgeUrlBox;
        private TextBox _workingDirectoryBox;
        private TextBox _launchCommandBox;
        private CheckBox _autoRefreshCheck;
        private CheckBox _simulationCheck;
        private CheckBox _confirmCheck;
        private CheckBox _verboseCheck;
        private ComboBox _architectureBox;
        private NumericUpDown _maxReplansInput;

        private Label _bridgeStatusValue;
        private Label _missionStatusValue;
        private Label _vehicleStatusValue;
        private Label _safetyStatusValue;
        private Label _modeValue;
        private Label _armedValue;
        private Label _altitudeValue;
        private Label _headingValue;
        private Label _batteryValue;
        private Label _groundspeedValue;
        private Label _positionValue;
        private Label _clarificationValue;

        private TextBox _taskBox;
        private Button _submitButton;
        private Button _approveButton;
        private Button _rejectButton;
        private Button _abortButton;
        private Button _rtlButton;
        private Button _repeatButton;
        private Button _refreshButton;
        private Button _startBridgeButton;
        private Button _stopBridgeButton;
        private Button _rebuildCommandButton;

        private TextBox _dslBox;
        private TextBox _reviewBox;
        private DataGridView _stepsGrid;
        private TextBox _eventsBox;

        private Color _okColor = Color.FromArgb(0, 120, 0);
        private Color _warnColor = Color.FromArgb(180, 110, 0);
        private Color _badColor = Color.FromArgb(180, 0, 0);
        private Color _mutedColor = SystemColors.GrayText;

        public TalkToFlyMissionControlForm(Form hostForm)
        {
            _hostForm = hostForm;
            _client = new TtfBridgeClient("http://127.0.0.1:8765");
            _refreshTimer = new System.Windows.Forms.Timer();
            _refreshTimer.Interval = 1250;
            _refreshTimer.Tick += delegate
            {
                if (_autoRefreshCheck != null && _autoRefreshCheck.Checked)
                {
                    RefreshSnapshot(false);
                }
            };

            BuildUi();
            ApplyMissionPlannerIntegration();
            RebuildLaunchCommand();
            _refreshTimer.Start();
            RefreshSnapshot(false);
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            _refreshTimer.Stop();
            if (_bridgeProcess != null && !_bridgeProcess.HasExited)
            {
                DialogResult result = MessageBox.Show(
                    this,
                    "The Talk-to-Fly bridge process was started by this plugin and is still running. Stop it now?",
                    "Talk-to-Fly bridge",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Question);
                if (result == DialogResult.Yes)
                {
                    StopBridgeProcess();
                }
            }
            base.OnFormClosing(e);
        }

        private void BuildUi()
        {
            Text = "Talk-to-Fly Mission Control v2.1.1";
            StartPosition = FormStartPosition.CenterScreen;
            Size = new Size(1320, 860);
            MinimumSize = new Size(1180, 760);
            Padding = new Padding(8);

            var root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.ColumnCount = 1;
            root.RowCount = 4;

            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 150));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 330));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 195));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            Controls.Add(root);

            root.Controls.Add(CreateSettingsAndStatusRow(), 0, 0);
            root.Controls.Add(CreateInputRow(), 0, 1);
            root.Controls.Add(CreatePlanRow(), 0, 2);
            root.Controls.Add(CreateEventsRow(), 0, 3);
        }

        private Control CreateSettingsAndStatusRow()
        {
            var row = new TableLayoutPanel();
            row.Dock = DockStyle.Fill;
            row.ColumnCount = 2;
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 47));
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 53));

            row.Controls.Add(CreateBridgeSettingsPanel(), 0, 0);
            row.Controls.Add(CreateStatusPanel(), 1, 0);
            return row;
        }

        private Control CreateBridgeSettingsPanel()
        {
            var group = new GroupBox();
            group.Text = "Bridge and runtime";
            group.Dock = DockStyle.Fill;

            var layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.Padding = new Padding(8, 6, 8, 8);
            layout.ColumnCount = 5;
            layout.RowCount = 4;
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 90));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 96));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 96));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 96));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
            group.Controls.Add(layout);

            layout.Controls.Add(CreateKeyLabel("Bridge URL"), 0, 0);
            _bridgeUrlBox = CreateSingleLineTextBox("http://127.0.0.1:8765");
            _bridgeUrlBox.TextChanged += delegate { _client = new TtfBridgeClient(_bridgeUrlBox.Text.Trim()); };
            layout.Controls.Add(_bridgeUrlBox, 1, 0);

            _refreshButton = CreateCompactButton("Refresh");
            _refreshButton.Click += delegate { RefreshSnapshot(true); };
            layout.Controls.Add(_refreshButton, 2, 0);

            _startBridgeButton = CreateCompactButton("Start bridge");
            _startBridgeButton.Click += delegate { StartBridgeProcess(); };
            layout.Controls.Add(_startBridgeButton, 3, 0);

            _stopBridgeButton = CreateCompactButton("Stop bridge");
            _stopBridgeButton.Click += delegate { StopBridgeProcess(); };
            layout.Controls.Add(_stopBridgeButton, 4, 0);

            layout.Controls.Add(CreateKeyLabel("Project dir"), 0, 1);
            _workingDirectoryBox = CreateSingleLineTextBox(GetDefaultProjectDirectory());
            layout.Controls.Add(_workingDirectoryBox, 1, 1);

            _simulationCheck = new CheckBox();
            _simulationCheck.Text = "SITL";
            _simulationCheck.Checked = true;
            _simulationCheck.Dock = DockStyle.Fill;
            _simulationCheck.CheckedChanged += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_simulationCheck, 2, 1);

            _confirmCheck = new CheckBox();
            _confirmCheck.Text = "Approval";
            _confirmCheck.Checked = true;
            _confirmCheck.Dock = DockStyle.Fill;
            _confirmCheck.CheckedChanged += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_confirmCheck, 3, 1);

            _verboseCheck = new CheckBox();
            _verboseCheck.Text = "Verbose";
            _verboseCheck.Dock = DockStyle.Fill;
            _verboseCheck.CheckedChanged += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_verboseCheck, 4, 1);

            layout.Controls.Add(CreateKeyLabel("Command"), 0, 2);
            _launchCommandBox = CreateSingleLineTextBox("");
            layout.Controls.Add(_launchCommandBox, 1, 2);

            _architectureBox = new ComboBox();
            _architectureBox.Dock = DockStyle.Fill;
            _architectureBox.DropDownStyle = ComboBoxStyle.DropDownList;
            _architectureBox.Items.Add("agentic");
            _architectureBox.Items.Add("one_shot");
            _architectureBox.SelectedIndex = 0;
            _architectureBox.SelectedIndexChanged += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_architectureBox, 2, 2);

            _maxReplansInput = new NumericUpDown();
            _maxReplansInput.Dock = DockStyle.Fill;
            _maxReplansInput.Minimum = 0;
            _maxReplansInput.Maximum = 10;
            _maxReplansInput.Value = 2;
            _maxReplansInput.ValueChanged += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_maxReplansInput, 3, 2);

            _rebuildCommandButton = CreateCompactButton("Build cmd");
            _rebuildCommandButton.Click += delegate { RebuildLaunchCommand(); };
            layout.Controls.Add(_rebuildCommandButton, 4, 2);

            layout.Controls.Add(CreateKeyLabel("Polling"), 0, 3);
            _autoRefreshCheck = new CheckBox();
            _autoRefreshCheck.Text = "Auto-refresh live bridge state";
            _autoRefreshCheck.Checked = true;
            _autoRefreshCheck.Dock = DockStyle.Fill;
            layout.Controls.Add(_autoRefreshCheck, 1, 3);

            var hint = CreateKeyLabel("Use -k");
            hint.TextAlign = ContentAlignment.MiddleCenter;
            hint.ForeColor = _warnColor;
            hint.ToolTipTextSafe("Bridge must run with -k / --confirm to pause for plugin approval.");
            layout.Controls.Add(hint, 2, 3);

            var versionLabel = CreateKeyLabel("v2.1.1");
            versionLabel.TextAlign = ContentAlignment.MiddleRight;
            versionLabel.ForeColor = _mutedColor;
            layout.Controls.Add(versionLabel, 4, 3);

            return group;
        }

        private Control CreateStatusPanel()
        {
            var group = new GroupBox();
            group.Text = "Live state";
            group.Dock = DockStyle.Fill;

            var layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.Padding = new Padding(8, 6, 8, 8);
            layout.ColumnCount = 4;
            layout.RowCount = 4;
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 92));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 92));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
            for (int i = 0; i < 4; i++) layout.RowStyles.Add(new RowStyle(SizeType.Percent, 25));
            group.Controls.Add(layout);

            _bridgeStatusValue = AddStatusPair(layout, 0, "Bridge", "Offline", "Mission", "No mission", out _missionStatusValue);
            _vehicleStatusValue = AddStatusPair(layout, 1, "Vehicle", "Unknown", "Safety", "Idle", out _safetyStatusValue);
            _modeValue = AddStatusPair(layout, 2, "Mode", "n/a", "Armed", "n/a", out _armedValue);
            _altitudeValue = AddStatusPair(layout, 3, "Alt", "n/a", "Heading", "n/a", out _headingValue);

            return group;
        }

        private Label AddStatusPair(TableLayoutPanel layout, int row, string keyA, string valA, string keyB, string valB, out Label valueB)
        {
            layout.Controls.Add(CreateKeyLabel(keyA), 0, row);
            var valueA = CreateValueLabel(valA);
            layout.Controls.Add(valueA, 1, row);
            layout.Controls.Add(CreateKeyLabel(keyB), 2, row);
            valueB = CreateValueLabel(valB);
            layout.Controls.Add(valueB, 3, row);
            return valueA;
        }

        private Control CreateInputRow()
        {
            var group = new GroupBox();
            group.Text = "Natural-language task / clarification";
            group.Dock = DockStyle.Fill;

            var layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.Padding = new Padding(8, 6, 8, 8);
            layout.ColumnCount = 1;
            layout.RowCount = 4;
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 50));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
            group.Controls.Add(layout);

            _clarificationValue = CreateValueLabel("No pending clarification. Enter a new UAV task.");
            layout.Controls.Add(_clarificationValue, 0, 0);

            var actionStrip = new TableLayoutPanel();
            actionStrip.Dock = DockStyle.Fill;
            actionStrip.ColumnCount = 6;
            actionStrip.RowCount = 1;

            for (int i = 0; i < 6; i++)
            {
                actionStrip.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f / 6f));
            }

            _submitButton = CreateButton("SUBMIT TASK");
            _submitButton.Click += delegate { SubmitTaskOrClarification(); };

            _approveButton = CreateButton("APPROVE PLAN");
            _approveButton.Click += delegate { PostSimpleAction("/approve", "Approving plan..."); };

            _rejectButton = CreateButton("REJECT PLAN");
            _rejectButton.Click += delegate { PostSimpleAction("/cancel", "Cancelling mission preview..."); };

            _abortButton = CreateButton("ABORT / LAND");
            _abortButton.Click += delegate { AbortOrLand(); };

            _repeatButton = CreateButton("REPEAT TASK");
            _repeatButton.Click += delegate { RepeatLastTask(); };

            var clearButton = CreateButton("CLEAR INPUT");
            clearButton.Click += delegate { _taskBox.Clear(); _taskBox.Focus(); };

            actionStrip.Controls.Add(_submitButton, 0, 0);
            actionStrip.Controls.Add(_approveButton, 1, 0);
            actionStrip.Controls.Add(_rejectButton, 2, 0);
            actionStrip.Controls.Add(_abortButton, 3, 0);
            actionStrip.Controls.Add(_repeatButton, 4, 0);
            actionStrip.Controls.Add(clearButton, 5, 0);
            layout.Controls.Add(actionStrip, 0, 1);

            _taskBox = new TextBox();
            _taskBox.Dock = DockStyle.Fill;
            _taskBox.Multiline = true;
            _taskBox.ScrollBars = ScrollBars.Vertical;
            _taskBox.AcceptsReturn = true;
            _taskBox.Text = "Take off to 10 metres, fly a 10 metre square, and land.";

            try
            {
                _taskBox.Font = new Font(_taskBox.Font.FontFamily, _taskBox.Font.Size * 2.0f, _taskBox.Font.Style);
            }
            catch
            {
                _taskBox.Font = new Font(FontFamily.GenericSansSerif, 18.0f, FontStyle.Regular);
            }

            _taskBox.KeyDown += delegate(object sender, KeyEventArgs e)
            {
                if (e.Control && e.KeyCode == Keys.Enter)
                {
                    e.SuppressKeyPress = true;
                    SubmitTaskOrClarification();
                }
            };
            layout.Controls.Add(_taskBox, 0, 2);

            var bottom = new TableLayoutPanel();
            bottom.Dock = DockStyle.Fill;
            bottom.ColumnCount = 5;
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 30));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 30));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 40));

            _rtlButton = CreateButton("RTL mode");
            _rtlButton.Click += delegate { SetMissionPlannerFlightMode("RTL"); };
            bottom.Controls.Add(_rtlButton, 0, 0);

            var landModeButton = CreateButton("LAND mode");
            landModeButton.Click += delegate { SetMissionPlannerFlightMode("LAND"); };
            bottom.Controls.Add(landModeButton, 1, 0);

            _batteryValue = CreateValueLabel("Battery: n/a");
            bottom.Controls.Add(_batteryValue, 2, 0);

            _groundspeedValue = CreateValueLabel("Groundspeed: n/a");
            bottom.Controls.Add(_groundspeedValue, 3, 0);

            _positionValue = CreateValueLabel("Position: n/a");
            bottom.Controls.Add(_positionValue, 4, 0);

            layout.Controls.Add(bottom, 0, 3);

            return group;
        }

        private Control CreatePlanRow()
        {
            var row = new TableLayoutPanel();
            row.Dock = DockStyle.Fill;
            row.ColumnCount = 2;
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
            row.Controls.Add(CreatePlanDetailsPanel(), 0, 0);
            row.Controls.Add(CreateStepGridPanel(), 1, 0);
            return row;
        }

        private Control CreatePlanDetailsPanel()
        {
            var group = new GroupBox();
            group.Text = "Reviewed DSL flight plan";
            group.Dock = DockStyle.Fill;

            var layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.Padding = new Padding(8, 6, 8, 8);
            layout.RowCount = 2;
            layout.ColumnCount = 1;
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 30));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 70));
            group.Controls.Add(layout);

            _dslBox = new TextBox();
            _dslBox.Dock = DockStyle.Fill;
            _dslBox.Multiline = true;
            _dslBox.ScrollBars = ScrollBars.Vertical;
            _dslBox.ReadOnly = true;
            _dslBox.Font = SafeMonospaceFont(10.0f);
            layout.Controls.Add(_dslBox, 0, 0);

            _reviewBox = new TextBox();
            _reviewBox.Dock = DockStyle.Fill;
            _reviewBox.Multiline = true;
            _reviewBox.ScrollBars = ScrollBars.Vertical;
            _reviewBox.ReadOnly = true;
            layout.Controls.Add(_reviewBox, 0, 1);

            return group;
        }

        private Control CreateStepGridPanel()
        {
            var group = new GroupBox();
            group.Text = "Compiled stepwise execution sequence";
            group.Dock = DockStyle.Fill;

            _stepsGrid = new DataGridView();
            _stepsGrid.Dock = DockStyle.Fill;
            _stepsGrid.ReadOnly = true;
            _stepsGrid.AllowUserToAddRows = false;
            _stepsGrid.AllowUserToDeleteRows = false;
            _stepsGrid.RowHeadersVisible = false;
            _stepsGrid.MultiSelect = false;
            _stepsGrid.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
            _stepsGrid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;
            _stepsGrid.EnableHeadersVisualStyles = true;
            _stepsGrid.Columns.Add(CreateGridColumn("StepNo", "#", 8));
            _stepsGrid.Columns.Add(CreateGridColumn("State", "State", 16));
            _stepsGrid.Columns.Add(CreateGridColumn("Action", "Compiled step", 48));
            _stepsGrid.Columns.Add(CreateGridColumn("Notes", "Notes", 28));
            group.Controls.Add(_stepsGrid);
            return group;
        }

        private Control CreateEventsRow()
        {
            var group = new GroupBox();
            group.Text = "Mission log and bridge events";
            group.Dock = DockStyle.Fill;

            _eventsBox = new TextBox();
            _eventsBox.Dock = DockStyle.Fill;
            _eventsBox.Multiline = true;
            _eventsBox.ScrollBars = ScrollBars.Vertical;
            _eventsBox.ReadOnly = true;
            _eventsBox.Font = SafeMonospaceFont(9.0f);
            group.Controls.Add(_eventsBox);
            return group;
        }

        private Label CreateKeyLabel(string text)
        {
            var label = new Label();
            label.Text = text;
            label.Dock = DockStyle.Fill;
            label.TextAlign = ContentAlignment.MiddleLeft;
            label.ForeColor = SystemColors.GrayText;
            return label;
        }

        private Label CreateValueLabel(string text)
        {
            var label = new Label();
            label.Text = text;
            label.Dock = DockStyle.Fill;
            label.TextAlign = ContentAlignment.MiddleLeft;
            label.AutoEllipsis = true;
            return label;
        }

        private TextBox CreateSingleLineTextBox(string text)
        {
            var box = new TextBox();
            box.Dock = DockStyle.Fill;
            box.Text = text;
            box.BorderStyle = BorderStyle.Fixed3D;
            return box;
        }

        private Button CreateButton(string text)
        {
            var button = new Button();
            button.Text = text;
            button.Dock = DockStyle.Fill;
            button.Margin = new Padding(4, 3, 4, 3);
            button.UseVisualStyleBackColor = false;
            button.FlatStyle = FlatStyle.Standard;
            button.TextAlign = ContentAlignment.MiddleCenter;
            button.AutoEllipsis = false;
            button.ForeColor = Color.Black;
            try { button.Font = new Font(button.Font, FontStyle.Bold); } catch { }
            return button;
        }

        private Button CreateCompactButton(string text)
        {
            var button = new Button();
            button.Text = text;
            button.Dock = DockStyle.Fill;
            button.Margin = new Padding(2, 1, 2, 1);
            button.UseVisualStyleBackColor = false;
            button.FlatStyle = FlatStyle.Standard;
            button.TextAlign = ContentAlignment.MiddleCenter;
            button.AutoEllipsis = false;
            button.ForeColor = Color.Black;

            try
            {
                button.Font = new Font(button.Font.FontFamily, Math.Max(7.5f, button.Font.Size), FontStyle.Regular);
            }
            catch { }

            return button;
        }

        private DataGridViewTextBoxColumn CreateGridColumn(string name, string header, float weight)
        {
            var col = new DataGridViewTextBoxColumn();
            col.Name = name;
            col.HeaderText = header;
            col.FillWeight = weight;
            col.SortMode = DataGridViewColumnSortMode.NotSortable;
            return col;
        }

        private Font SafeMonospaceFont(float size)
        {
            try { return new Font("Consolas", size, FontStyle.Regular); }
            catch { return new Font(FontFamily.GenericMonospace, size, FontStyle.Regular); }
        }

        private void ApplyMissionPlannerIntegration()
        {
            if (_hostForm != null)
            {
                try { Icon = _hostForm.Icon; } catch { }
                try { Font = _hostForm.Font; } catch { }
            }

            TryApplyMissionPlannerTheme();
        }

        private void TryApplyMissionPlannerTheme()
        {
            try
            {
                Type themeType = Type.GetType("MissionPlanner.Utilities.ThemeManager, MissionPlanner");
                if (themeType == null) return;
                MethodInfo mi = themeType.GetMethod(
                    "ApplyThemeTo",
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new Type[] { typeof(Control) },
                    null);
                if (mi != null)
                {
                    mi.Invoke(null, new object[] { this });
                }
            }
            catch { }
        }

        private string GetDefaultProjectDirectory()
        {
            string capstoneProject = "/home/jack/Capstone/ardupilot";
            if (!IsWindows() && Directory.Exists(capstoneProject))
            {
                return capstoneProject;
            }
            return Directory.GetCurrentDirectory();
        }

        private string GetDefaultVenvActivatePath()
        {
            return "/home/jack/Capstone/ardupilot/venv/ardupilot/bin/activate";
        }

        private string GetDefaultPythonLaunchPrefix()
        {
            if (!IsWindows())
            {
                return "source " + QuoteForShellToken(GetDefaultVenvActivatePath()) + " && python";
            }
            return "python";
        }

        private string QuoteForShellToken(string text)
        {
            if (string.IsNullOrEmpty(text)) return "''";
            if (IsWindows())
            {
                return "\"" + text.Replace("\"", "\\\"") + "\"";
            }
            return "'" + text.Replace("'", "'\\''") + "'";
        }

        private string QuoteForProcessArgument(string text)
        {
            if (text == null) return "\"\"";
            return "\"" + text.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }

        private void RebuildLaunchCommand()
        {
            if (_launchCommandBox == null) return;

            string url = _bridgeUrlBox == null ? "http://127.0.0.1:8765" : _bridgeUrlBox.Text.Trim();
            string host = "127.0.0.1";
            int port = 8765;
            try
            {
                Uri uri = new Uri(url);
                host = uri.Host;
                port = uri.Port > 0 ? uri.Port : 8765;
            }
            catch { }

            string architecture = _architectureBox == null || _architectureBox.SelectedItem == null
                ? "agentic"
                : Convert.ToString(_architectureBox.SelectedItem, CultureInfo.InvariantCulture);

            var args = new StringBuilder();
            args.Append(GetDefaultPythonLaunchPrefix()).Append(" -m talk_to_fly.bridge");
            args.Append(" --bridge-host ").Append(host);
            args.Append(" --bridge-port ").Append(port.ToString(CultureInfo.InvariantCulture));
            if (_simulationCheck == null || _simulationCheck.Checked) args.Append(" -s");
            if (_confirmCheck == null || _confirmCheck.Checked) args.Append(" -k");
            if (_verboseCheck != null && _verboseCheck.Checked) args.Append(" -v");
            args.Append(" --architecture ").Append(architecture);
            if (architecture != "one_shot")
            {
                args.Append(" --max-replans ").Append(((int)(_maxReplansInput == null ? 2 : _maxReplansInput.Value)).ToString(CultureInfo.InvariantCulture));
            }
            _launchCommandBox.Text = args.ToString();
        }

        private string NormaliseLaunchCommand(string command)
        {
            if (command == null) return "";

            string trimmed = command.Trim();
            if (!IsWindows() && trimmed.StartsWith("poetry run python", StringComparison.OrdinalIgnoreCase))
            {
                return GetDefaultPythonLaunchPrefix() + trimmed.Substring("poetry run python".Length);
            }

            return trimmed;
        }

        private void StartBridgeProcess()
        {
            try
            {
                if (_bridgeProcess != null && !_bridgeProcess.HasExited)
                {
                    AppendEventLine("Bridge process is already running.");
                    return;
                }

                string command = NormaliseLaunchCommand(_launchCommandBox.Text);
                if (command.Length == 0)
                {
                    MessageBox.Show(this, "Bridge launch command is empty.", "Talk-to-Fly", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }

                string workingDir = _workingDirectoryBox.Text.Trim();
                if (workingDir.Length == 0 || !Directory.Exists(workingDir))
                {
                    workingDir = Directory.GetCurrentDirectory();
                }

                var psi = new ProcessStartInfo();
                if (IsWindows())
                {
                    psi.FileName = "cmd.exe";
                    psi.Arguments = "/C " + QuoteForProcessArgument(command);
                }
                else
                {
                    psi.FileName = "/bin/bash";
                    psi.Arguments = "-lc " + QuoteForProcessArgument(command);
                }
                psi.WorkingDirectory = workingDir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;

                string srcPath = Path.Combine(workingDir, "src");
                if (Directory.Exists(srcPath))
                {
                    string oldPythonPath = psi.EnvironmentVariables.ContainsKey("PYTHONPATH") ? psi.EnvironmentVariables["PYTHONPATH"] : "";
                    psi.EnvironmentVariables["PYTHONPATH"] = oldPythonPath.Length == 0 ? srcPath : srcPath + Path.PathSeparator + oldPythonPath;
                }

                _bridgeProcess = new Process();
                _bridgeProcess.StartInfo = psi;
                _bridgeProcess.EnableRaisingEvents = true;
                _bridgeProcess.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data != null) AppendEventLine("[bridge] " + e.Data);
                };
                _bridgeProcess.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data != null) AppendEventLine("[bridge] " + e.Data);
                };
                _bridgeProcess.Exited += delegate
                {
                    AppendEventLine("Bridge process exited.");
                    BeginInvokeSafe(delegate { UpdateBridgeProcessButtons(); });
                };
                _bridgeProcess.Start();
                _bridgeProcess.BeginOutputReadLine();
                _bridgeProcess.BeginErrorReadLine();

                AppendEventLine("Started bridge: " + command);
                UpdateBridgeProcessButtons();
            }
            catch (Exception ex)
            {
                AppendEventLine("Could not start bridge: " + ex.Message);
                MessageBox.Show(this, ex.Message, "Talk-to-Fly bridge start failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private void StopBridgeProcess()
        {
            try
            {
                if (_bridgeProcess == null || _bridgeProcess.HasExited)
                {
                    AppendEventLine("No plugin-started bridge process is running.");
                    UpdateBridgeProcessButtons();
                    return;
                }
                _bridgeProcess.Kill();
                _bridgeProcess.WaitForExit(2000);
                AppendEventLine("Stopped bridge process.");
            }
            catch (Exception ex)
            {
                AppendEventLine("Could not stop bridge: " + ex.Message);
            }
            finally
            {
                UpdateBridgeProcessButtons();
            }
        }

        private void UpdateBridgeProcessButtons()
        {
            bool running = _bridgeProcess != null && !_bridgeProcess.HasExited;
            if (_startBridgeButton != null) _startBridgeButton.Enabled = !running;
            if (_stopBridgeButton != null) _stopBridgeButton.Enabled = running;
        }

        private bool IsWindows()
        {
            PlatformID p = Environment.OSVersion.Platform;
            return p == PlatformID.Win32NT || p == PlatformID.Win32Windows || p == PlatformID.Win32S || p == PlatformID.WinCE;
        }

        private void RefreshSnapshot(bool userInitiated)
        {
            if (_refreshInProgress) return;
            _refreshInProgress = true;
            RunBridgeAction(
                userInitiated ? "Refreshing bridge state..." : null,
                delegate { return _client.Get("/status"); },
                delegate(TtfBridgeResponse response)
                {
                    _refreshInProgress = false;
                    HandleBridgeResponse(response, userInitiated);
                },
                delegate(Exception ex)
                {
                    _refreshInProgress = false;
                    _bridgeOnline = false;
                    if (userInitiated) AppendEventLine("Bridge offline: " + ex.Message);
                    UpdateOfflineUi(ex.Message);
                },
                false);
        }

        private void SubmitTaskOrClarification()
        {
            string text = _taskBox.Text.Trim();
            if (text.Length == 0)
            {
                MessageBox.Show(this, "Enter a task description or clarification answer first.", "Talk-to-Fly", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            bool clarification = IsClarificationPending();
            if (!clarification)
            {
                _lastNaturalLanguageTask = text;
            }

            string endpoint = clarification ? "/clarification" : "/task";
            string key = clarification ? "Answer" : "Task";
            RunBridgeAction(
                clarification ? "Sending clarification..." : "Submitting task...",
                delegate { return _client.PostForm(endpoint, key, text); },
                delegate(TtfBridgeResponse response)
                {
                    HandleBridgeResponse(response, true);
                    if (response.Ok)
                    {
                        _taskBox.Clear();
                    }
                },
                null,
                true);
        }

        private void RepeatLastTask()
        {
            if (_lastNaturalLanguageTask == null || _lastNaturalLanguageTask.Trim().Length == 0)
            {
                MessageBox.Show(this, "There is no previous natural-language task to repeat.", "Talk-to-Fly", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            _taskBox.Text = _lastNaturalLanguageTask;
            SubmitTaskOrClarification();
        }

        private void PostSimpleAction(string endpoint, string busyText)
        {
            RunBridgeAction(
                busyText,
                delegate { return _client.PostForm(endpoint, null, null); },
                delegate(TtfBridgeResponse response) { HandleBridgeResponse(response, true); },
                null,
                true);
        }

        private void AbortOrLand()
        {
            if (_lastSnapshot != null && GetDict(_lastSnapshot, "Mission") != null)
            {
                PostSimpleAction("/abort", "Requesting abort/land through Talk-to-Fly bridge...");
            }
            else
            {
                SetMissionPlannerFlightMode("LAND");
            }
        }

        private void SetMissionPlannerFlightMode(string mode)
        {
            try
            {
                if (TrySetModeViaReflection(mode))
                {
                    AppendEventLine("Mission Planner mode command sent: " + mode);
                }
                else
                {
                    AppendEventLine("Could not find Mission Planner comPort.setMode(). Use the bridge task input or Mission Planner flight controls instead.");
                    MessageBox.Show(this, "Could not find Mission Planner comPort.setMode() by reflection.", "Talk-to-Fly", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
            catch (Exception ex)
            {
                AppendEventLine("Mode command failed: " + ex.Message);
                MessageBox.Show(this, ex.Message, "Mission Planner mode command failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private bool TrySetModeViaReflection(string mode)
        {
            var candidates = new List<object>();

            try
            {
                Type mainV2Type = Type.GetType("MissionPlanner.MainV2, MissionPlanner");
                if (mainV2Type != null)
                {
                    FieldInfo fi = mainV2Type.GetField("comPort", BindingFlags.Public | BindingFlags.Static);
                    if (fi != null) candidates.Add(fi.GetValue(null));
                    PropertyInfo pi = mainV2Type.GetProperty("comPort", BindingFlags.Public | BindingFlags.Static);
                    if (pi != null) candidates.Add(pi.GetValue(null, null));
                }
            }
            catch { }

            foreach (object candidate in candidates)
            {
                if (candidate == null) continue;
                MethodInfo mi = candidate.GetType().GetMethod("setMode", new Type[] { typeof(string) });
                if (mi != null)
                {
                    mi.Invoke(candidate, new object[] { mode });
                    return true;
                }
                mi = candidate.GetType().GetMethod("SetMode", new Type[] { typeof(string) });
                if (mi != null)
                {
                    mi.Invoke(candidate, new object[] { mode });
                    return true;
                }
            }
            return false;
        }

        private void RunBridgeAction(string busyText, Func<TtfBridgeResponse> action, Action<TtfBridgeResponse> onSuccess, Action<Exception> onError, bool disableButtons)
        {
            if (busyText != null && busyText.Length > 0) AppendEventLine(busyText);
            if (disableButtons) SetActionButtonsEnabled(false);

            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    lock (_requestLock)
                    {
                        TtfBridgeResponse response = action();
                        BeginInvokeSafe(delegate
                        {
                            if (disableButtons) SetActionButtonsEnabled(true);
                            if (onSuccess != null) onSuccess(response);
                        });
                    }
                }
                catch (Exception ex)
                {
                    BeginInvokeSafe(delegate
                    {
                        if (disableButtons) SetActionButtonsEnabled(true);
                        if (onError != null) onError(ex);
                        else
                        {
                            AppendEventLine("Bridge request failed: " + ex.Message);
                            UpdateOfflineUi(ex.Message);
                        }
                    });
                }
            });
        }

        private void HandleBridgeResponse(TtfBridgeResponse response, bool showErrors)
        {
            _bridgeOnline = true;
            if (response.Snapshot != null)
            {
                _lastSnapshot = response.Snapshot;
                UpdateFromSnapshot(response.Snapshot);
            }

            if (!response.Ok && showErrors)
            {
                AppendEventLine("Bridge error: " + response.Error);
                MessageBox.Show(this, response.Error ?? "Unknown bridge error.", "Talk-to-Fly bridge", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private void UpdateOfflineUi(string reason)
        {
            _bridgeOnline = false;
            _bridgeStatusValue.Text = "Offline";
            _bridgeStatusValue.ForeColor = _badColor;
            _missionStatusValue.Text = "No bridge";
            _vehicleStatusValue.Text = "Unknown";
            _safetyStatusValue.Text = reason;
            _safetyStatusValue.ForeColor = _mutedColor;

            _submitButton.Enabled = true;
            _approveButton.Enabled = true;
            _rejectButton.Enabled = true;
            _abortButton.Enabled = true;
            _repeatButton.Enabled = true;

            UpdateBridgeProcessButtons();
        }

        private void UpdateFromSnapshot(IDictionary<string, object> snapshot)
        {
            IDictionary<string, object> bridge = GetDict(snapshot, "Bridge");
            IDictionary<string, object> mission = GetDict(snapshot, "Mission");
            IDictionary<string, object> drone = GetDict(snapshot, "Drone");
            IDictionary<string, object> conversation = GetDict(snapshot, "Conversation");
            IList<object> events = GetList(snapshot, "Events");

            bool workerAlive = GetBool(bridge, "WorkerAlive", false);
            bool approvalRequired = GetBool(bridge, "ApprovalRequired", false);
            bool confirm = GetBool(bridge, "Confirm", false);
            string connect = GetString(bridge, "Connect", "");
            string bridgeText = _bridgeOnline ? "Online" : "Offline";
            if (workerAlive) bridgeText += " / busy";
            _bridgeStatusValue.Text = bridgeText;
            _bridgeStatusValue.ForeColor = _bridgeOnline ? _okColor : _badColor;

            string missionStatus = mission == null ? "No mission" : GetString(mission, "Status", "unknown");
            _missionStatusValue.Text = missionStatus;
            _missionStatusValue.ForeColor = ColorForStatus(missionStatus);

            string flightMode = GetString(drone, "FlightMode", "n/a");
            string armed = FormatBool(GetNullableBool(drone, "Armed"));
            _vehicleStatusValue.Text = flightMode + " / armed " + armed;
            _vehicleStatusValue.ForeColor = _okColor;
            _modeValue.Text = flightMode;
            _armedValue.Text = armed;

            _altitudeValue.Text = FormatDouble(GetNullableDouble(drone, "PositionAlt"), " m", 1);
            _headingValue.Text = FormatDouble(GetNullableDouble(drone, "HeadingDegrees"), "°", 1);
            _batteryValue.Text = "Battery: " + FormatDouble(GetNullableDouble(drone, "BatteryPercent"), "%", 1);
            _groundspeedValue.Text = "Groundspeed: " + FormatDouble(GetNullableDouble(drone, "GroundspeedMps"), " m/s", 2);
            string lat = FormatDouble(GetNullableDouble(drone, "PositionLat"), "", 6);
            string lon = FormatDouble(GetNullableDouble(drone, "PositionLon"), "", 6);
            _positionValue.Text = "Position: " + lat + ", " + lon;

            bool pendingClarification = GetBool(conversation, "PendingClarification", false) || missionStatus == "awaiting_clarification";
            string pendingQuestion = GetString(conversation, "PendingQuestion", null);
            if (pendingQuestion == null && mission != null) pendingQuestion = GetString(mission, "PendingQuestion", null);

            if (pendingClarification)
            {
                _clarificationValue.Text = "Clarification required: " + (pendingQuestion ?? "answer the pending planner question");
                _clarificationValue.ForeColor = _warnColor;
                _submitButton.Text = "SEND CLARIFICATION";
            }
            else
            {
                _clarificationValue.Text = "No pending clarification. Enter a new UAV task.";
                _clarificationValue.ForeColor = SystemColors.ControlText;
                _submitButton.Text = "SUBMIT TASK";
            }

            if (!approvalRequired || !confirm)
            {
                _safetyStatusValue.Text = "Bridge not in approval mode (-k not active)";
                _safetyStatusValue.ForeColor = _warnColor;
            }
            else if (missionStatus == "awaiting_approval")
            {
                _safetyStatusValue.Text = "Operator approval required";
                _safetyStatusValue.ForeColor = _warnColor;
            }
            else if (workerAlive || missionStatus == "executing")
            {
                _safetyStatusValue.Text = "Mission executing";
                _safetyStatusValue.ForeColor = _okColor;
            }
            else
            {
                _safetyStatusValue.Text = "Ready";
                _safetyStatusValue.ForeColor = _okColor;
            }

            UpdatePlanBoxes(mission, connect, approvalRequired, confirm, workerAlive);
            UpdateStepGrid(mission);
            AppendNewEvents(events);
            UpdateButtonsForStatus(missionStatus, workerAlive, pendingClarification);
            UpdateBridgeProcessButtons();
        }

        private void UpdatePlanBoxes(IDictionary<string, object> mission, string connect, bool approvalRequired, bool confirm, bool workerAlive)
        {
            if (mission == null)
            {
                _dslBox.Text = "";
                _reviewBox.Text = "No active Talk-to-Fly mission.\r\n\r\nBridge connection: " + (connect ?? "n/a");
                return;
            }

            _dslBox.Text = GetString(mission, "CurrentPlan", "");

            var sb = new StringBuilder();
            sb.AppendLine("Mission ID       : " + GetString(mission, "MissionId", "n/a"));
            sb.AppendLine("User task        : " + GetString(mission, "UserTask", "n/a"));
            sb.AppendLine("Status           : " + GetString(mission, "Status", "n/a"));
            sb.AppendLine("Plan source      : " + GetString(mission, "CurrentPlanSource", "n/a"));
            sb.AppendLine("Bridge connect   : " + (connect ?? "n/a"));
            sb.AppendLine("Approval gate    : " + (approvalRequired && confirm ? "enabled" : "disabled"));
            sb.AppendLine("Worker active    : " + workerAlive.ToString());
            sb.AppendLine("Replans          : " + GetInt(mission, "ReplanCount", 0) + "/" + GetInt(mission, "MaxReplans", 0));
            sb.AppendLine("Local recoveries : " + GetInt(mission, "LocalRecoveryCount", 0));
            sb.AppendLine("Completed steps  : " + CountList(GetList(mission, "CompletedSteps")));
            sb.AppendLine("Next step index  : " + GetInt(mission, "NextStepIdx", 0));

            string review = GetString(mission, "ReviewSummary", null);
            if (review != null && review.Trim().Length > 0)
            {
                sb.AppendLine();
                sb.AppendLine("Plan review:");
                sb.AppendLine(review);
            }

            string question = GetString(mission, "PendingQuestion", null);
            if (question != null && question.Trim().Length > 0)
            {
                sb.AppendLine();
                sb.AppendLine("Pending clarification:");
                sb.AppendLine(question);
            }

            string failure = GetString(mission, "LastFailureReason", null);
            if (failure != null && failure.Trim().Length > 0)
            {
                sb.AppendLine();
                sb.AppendLine("Last failure:");
                sb.AppendLine(failure);
            }

            _reviewBox.Text = sb.ToString();
        }

        private void UpdateStepGrid(IDictionary<string, object> mission)
        {
            _stepsGrid.Rows.Clear();
            if (mission == null) return;

            IList<object> steps = GetList(mission, "CurrentPlanSteps");
            IList<object> completed = GetList(mission, "CompletedSteps");
            string status = GetString(mission, "Status", "");
            int next = GetInt(mission, "NextStepIdx", 0);

            if (steps == null || steps.Count == 0)
            {
                string plan = GetString(mission, "CurrentPlan", "");
                if (plan.Trim().Length > 0)
                {
                    _stepsGrid.Rows.Add("--", "Plan", "DSL shown at left", "Waiting for dry-run compilation preview");
                }
                return;
            }

            for (int i = 0; i < steps.Count; i++)
            {
                string step = Convert.ToString(steps[i], CultureInfo.InvariantCulture);
                string state;
                string notes = "";

                if (i < next || ListContainsString(completed, step))
                {
                    state = "Done";
                    notes = "Verified by monitor";
                }
                else if ((status == "executing" || status == "replanning") && i == next)
                {
                    state = "Active";
                    notes = "Current or next command";
                }
                else if (status == "awaiting_approval")
                {
                    state = "Queued";
                    notes = "Waiting for operator approval";
                }
                else if (status == "failed")
                {
                    state = i == next ? "Failed" : "Unrun";
                    notes = i == next ? "Failure or replan point" : "Not executed";
                }
                else if (status == "completed")
                {
                    state = "Done";
                    notes = "Mission completed";
                }
                else
                {
                    state = "Queued";
                }

                int rowIndex = _stepsGrid.Rows.Add((i + 1).ToString("00", CultureInfo.InvariantCulture), state, step, notes);
                DataGridViewRow row = _stepsGrid.Rows[rowIndex];
                if (state == "Done") row.Cells[1].Style.ForeColor = _okColor;
                else if (state == "Active") row.Cells[1].Style.ForeColor = _warnColor;
                else if (state == "Failed") row.Cells[1].Style.ForeColor = _badColor;
                else row.Cells[1].Style.ForeColor = SystemColors.ControlText;
            }
        }

        private void AppendNewEvents(IList<object> events)
        {
            if (events == null) return;
            foreach (object item in events)
            {
                IDictionary<string, object> evt = item as IDictionary<string, object>;
                if (evt == null) continue;

                int seq = GetInt(evt, "Seq", 0);
                if (seq <= _lastEventSeq) continue;
                _lastEventSeq = seq;

                double ts = GetDouble(evt, "Timestamp", 0.0);
                string when = UnixTimeToLocalString(ts);
                string level = GetString(evt, "Level", "info");
                string missionId = GetString(evt, "MissionId", "");
                string message = GetString(evt, "Message", "");

                AppendEventLine("[" + when + "] [" + level.ToUpperInvariant() + "]" +
                    (missionId.Length > 0 ? " [" + missionId + "] " : " ") + message);
            }
        }

        private void AppendEventLine(string line)
        {
            BeginInvokeSafe(delegate
            {
                if (_eventsBox == null || _eventsBox.IsDisposed) return;
                if (_eventsBox.TextLength > 60000)
                {
                    _eventsBox.Text = _eventsBox.Text.Substring(Math.Max(0, _eventsBox.TextLength - 40000));
                }
                _eventsBox.AppendText(line + Environment.NewLine);
            });
        }

        private void BeginInvokeSafe(MethodInvoker action)
        {
            if (IsDisposed) return;
            try
            {
                if (InvokeRequired) BeginInvoke(action);
                else action();
            }
            catch { }
        }

        private void SetActionButtonsEnabled(bool enabled)
        {
            _submitButton.Enabled = true;
            _approveButton.Enabled = true;
            _rejectButton.Enabled = true;
            _abortButton.Enabled = true;
            _repeatButton.Enabled = true;
            if (_refreshButton != null) _refreshButton.Enabled = enabled;
        }

        private void UpdateButtonsForStatus(string status, bool workerAlive, bool pendingClarification)
        {
            _submitButton.Enabled = true;
            _approveButton.Enabled = true;
            _rejectButton.Enabled = true;
            _abortButton.Enabled = true;
            _rtlButton.Enabled = true;
            _repeatButton.Enabled = true;
        }

        private bool IsClarificationPending()
        {
            if (_lastSnapshot == null) return false;
            IDictionary<string, object> conversation = GetDict(_lastSnapshot, "Conversation");
            IDictionary<string, object> mission = GetDict(_lastSnapshot, "Mission");
            return GetBool(conversation, "PendingClarification", false) || GetString(mission, "Status", "") == "awaiting_clarification";
        }

        private Color ColorForStatus(string status)
        {
            if (status == null) return _mutedColor;
            switch (status)
            {
                case "completed": return _okColor;
                case "executing": return _okColor;
                case "awaiting_approval": return _warnColor;
                case "awaiting_clarification": return _warnColor;
                case "replanning": return _warnColor;
                case "failed": return _badColor;
                case "cancelled": return _badColor;
                default: return _mutedColor;
            }
        }

        private string FormatDouble(double? value, string suffix, int decimals)
        {
            if (!value.HasValue) return "n/a";
            return value.Value.ToString("F" + decimals.ToString(CultureInfo.InvariantCulture), CultureInfo.InvariantCulture) + suffix;
        }

        private string FormatBool(bool? value)
        {
            if (!value.HasValue) return "n/a";
            return value.Value ? "yes" : "no";
        }

        private string UnixTimeToLocalString(double seconds)
        {
            try
            {
                DateTime epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
                return epoch.AddSeconds(seconds).ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);
            }
            catch
            {
                return DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
            }
        }

        private int CountList(IList<object> list)
        {
            return list == null ? 0 : list.Count;
        }

        private bool ListContainsString(IList<object> list, string value)
        {
            if (list == null) return false;
            foreach (object obj in list)
            {
                if (Convert.ToString(obj, CultureInfo.InvariantCulture) == value) return true;
            }
            return false;
        }

        private static IDictionary<string, object> GetDict(IDictionary<string, object> dict, string key)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return null;
            return dict[key] as IDictionary<string, object>;
        }

        private static IList<object> GetList(IDictionary<string, object> dict, string key)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return null;
            return dict[key] as IList<object>;
        }

        private static string GetString(IDictionary<string, object> dict, string key, string fallback)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return fallback;
            return Convert.ToString(dict[key], CultureInfo.InvariantCulture);
        }

        private static bool GetBool(IDictionary<string, object> dict, string key, bool fallback)
        {
            bool? val = GetNullableBool(dict, key);
            return val.HasValue ? val.Value : fallback;
        }

        private static bool? GetNullableBool(IDictionary<string, object> dict, string key)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return null;
            object obj = dict[key];
            if (obj is bool) return (bool)obj;
            string s = Convert.ToString(obj, CultureInfo.InvariantCulture);
            bool parsed;
            if (bool.TryParse(s, out parsed)) return parsed;
            return null;
        }

        private static int GetInt(IDictionary<string, object> dict, string key, int fallback)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return fallback;
            try { return Convert.ToInt32(dict[key], CultureInfo.InvariantCulture); }
            catch { return fallback; }
        }

        private static double GetDouble(IDictionary<string, object> dict, string key, double fallback)
        {
            double? val = GetNullableDouble(dict, key);
            return val.HasValue ? val.Value : fallback;
        }

        private static double? GetNullableDouble(IDictionary<string, object> dict, string key)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return null;
            try { return Convert.ToDouble(dict[key], CultureInfo.InvariantCulture); }
            catch { return null; }
        }
    }

    internal sealed class TtfBridgeResponse
    {
        public bool Ok;
        public string Error;
        public IDictionary<string, object> Snapshot;
        public string RawJson;
    }

    internal sealed class TtfBridgeClient
    {
        private readonly string _baseUrl;

        public TtfBridgeClient(string baseUrl)
        {
            _baseUrl = NormalizeBaseUrl(baseUrl);
        }

        public TtfBridgeResponse Get(string endpoint)
        {
            string json = Send("GET", endpoint, null);
            return ParseResponse(json);
        }

        public TtfBridgeResponse PostForm(string endpoint, string key, string value)
        {
            string body = "";
            if (key != null)
            {
                body = Uri.EscapeDataString(key) + "=" + Uri.EscapeDataString(value ?? "");
            }
            string json = Send("POST", endpoint, body);
            return ParseResponse(json);
        }

        private string Send(string method, string endpoint, string body)
        {
            string url = _baseUrl + endpoint;
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = method;
            req.Timeout = 15000;
            req.ReadWriteTimeout = 15000;
            req.UserAgent = "TalkToFlyMissionPlannerPlugin/2.1.1";

            if (method == "POST")
            {
                byte[] bytes = Encoding.UTF8.GetBytes(body ?? "");
                req.ContentType = "application/x-www-form-urlencoded; charset=utf-8";
                req.ContentLength = bytes.Length;
                using (Stream stream = req.GetRequestStream())
                {
                    stream.Write(bytes, 0, bytes.Length);
                }
            }

            try
            {
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                using (Stream stream = resp.GetResponseStream())
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                {
                    return reader.ReadToEnd();
                }
            }
            catch (WebException ex)
            {
                if (ex.Response != null)
                {
                    using (Stream stream = ex.Response.GetResponseStream())
                    using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                    {
                        return reader.ReadToEnd();
                    }
                }
                throw;
            }
        }

        private TtfBridgeResponse ParseResponse(string json)
        {
            object parsed = MiniJson.Parse(json);
            IDictionary<string, object> root = parsed as IDictionary<string, object>;
            if (root == null) throw new InvalidOperationException("Bridge returned non-object JSON.");

            var response = new TtfBridgeResponse();
            response.RawJson = json;
            response.Ok = GetBool(root, "Ok", false);
            response.Error = GetString(root, "Error", null);
            response.Snapshot = GetDict(root, "Snapshot");
            return response;
        }

        private static string NormalizeBaseUrl(string value)
        {
            string s = (value ?? "").Trim();
            if (s.Length == 0) s = "http://127.0.0.1:8765";
            if (!s.StartsWith("http://", StringComparison.OrdinalIgnoreCase) && !s.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                s = "http://" + s;
            }
            while (s.EndsWith("/", StringComparison.Ordinal)) s = s.Substring(0, s.Length - 1);
            return s;
        }

        private static IDictionary<string, object> GetDict(IDictionary<string, object> dict, string key)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return null;
            return dict[key] as IDictionary<string, object>;
        }

        private static string GetString(IDictionary<string, object> dict, string key, string fallback)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return fallback;
            return Convert.ToString(dict[key], CultureInfo.InvariantCulture);
        }

        private static bool GetBool(IDictionary<string, object> dict, string key, bool fallback)
        {
            if (dict == null || !dict.ContainsKey(key) || dict[key] == null) return fallback;
            object obj = dict[key];
            if (obj is bool) return (bool)obj;
            bool parsed;
            if (bool.TryParse(Convert.ToString(obj, CultureInfo.InvariantCulture), out parsed)) return parsed;
            return fallback;
        }
    }

    internal static class MiniJson
    {
        public static object Parse(string json)
        {
            if (json == null) throw new ArgumentNullException("json");
            var parser = new Parser(json);
            return parser.ParseValue();
        }

        private sealed class Parser
        {
            private readonly string _json;
            private int _index;

            public Parser(string json)
            {
                _json = json;
                _index = 0;
            }

            public object ParseValue()
            {
                SkipWhiteSpace();
                if (_index >= _json.Length) return null;
                char c = _json[_index];
                if (c == '{') return ParseObject();
                if (c == '[') return ParseArray();
                if (c == '"') return ParseString();
                if (c == 't') return ParseLiteral("true", true);
                if (c == 'f') return ParseLiteral("false", false);
                if (c == 'n') return ParseLiteral("null", null);
                return ParseNumber();
            }

            private IDictionary<string, object> ParseObject()
            {
                var dict = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                Expect('{');
                SkipWhiteSpace();
                if (Peek('}'))
                {
                    _index++;
                    return dict;
                }

                while (true)
                {
                    SkipWhiteSpace();
                    string key = ParseString();
                    SkipWhiteSpace();
                    Expect(':');
                    object value = ParseValue();
                    dict[key] = value;
                    SkipWhiteSpace();
                    if (Peek('}'))
                    {
                        _index++;
                        break;
                    }
                    Expect(',');
                }
                return dict;
            }

            private IList<object> ParseArray()
            {
                var list = new List<object>();
                Expect('[');
                SkipWhiteSpace();
                if (Peek(']'))
                {
                    _index++;
                    return list;
                }

                while (true)
                {
                    list.Add(ParseValue());
                    SkipWhiteSpace();
                    if (Peek(']'))
                    {
                        _index++;
                        break;
                    }
                    Expect(',');
                }
                return list;
            }

            private string ParseString()
            {
                Expect('"');
                var sb = new StringBuilder();
                while (_index < _json.Length)
                {
                    char c = _json[_index++];
                    if (c == '"') return sb.ToString();
                    if (c == '\\')
                    {
                        if (_index >= _json.Length) break;
                        char esc = _json[_index++];
                        switch (esc)
                        {
                            case '"': sb.Append('"'); break;
                            case '\\': sb.Append('\\'); break;
                            case '/': sb.Append('/'); break;
                            case 'b': sb.Append('\b'); break;
                            case 'f': sb.Append('\f'); break;
                            case 'n': sb.Append('\n'); break;
                            case 'r': sb.Append('\r'); break;
                            case 't': sb.Append('\t'); break;
                            case 'u':
                                if (_index + 4 <= _json.Length)
                                {
                                    string hex = _json.Substring(_index, 4);
                                    int code = int.Parse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                                    sb.Append((char)code);
                                    _index += 4;
                                }
                                break;
                            default:
                                sb.Append(esc);
                                break;
                        }
                    }
                    else
                    {
                        sb.Append(c);
                    }
                }
                throw new FormatException("Unterminated JSON string.");
            }

            private object ParseNumber()
            {
                int start = _index;
                if (_index < _json.Length && _json[_index] == '-') _index++;
                while (_index < _json.Length && char.IsDigit(_json[_index])) _index++;
                bool isFloat = false;
                if (_index < _json.Length && _json[_index] == '.')
                {
                    isFloat = true;
                    _index++;
                    while (_index < _json.Length && char.IsDigit(_json[_index])) _index++;
                }
                if (_index < _json.Length && (_json[_index] == 'e' || _json[_index] == 'E'))
                {
                    isFloat = true;
                    _index++;
                    if (_index < _json.Length && (_json[_index] == '+' || _json[_index] == '-')) _index++;
                    while (_index < _json.Length && char.IsDigit(_json[_index])) _index++;
                }

                string token = _json.Substring(start, _index - start);
                if (isFloat)
                {
                    double d;
                    if (double.TryParse(token, NumberStyles.Float, CultureInfo.InvariantCulture, out d)) return d;
                }
                else
                {
                    long l;
                    if (long.TryParse(token, NumberStyles.Integer, CultureInfo.InvariantCulture, out l)) return l;
                }
                throw new FormatException("Invalid JSON number: " + token);
            }

            private object ParseLiteral(string literal, object value)
            {
                if (_index + literal.Length > _json.Length || _json.Substring(_index, literal.Length) != literal)
                {
                    throw new FormatException("Invalid JSON literal.");
                }
                _index += literal.Length;
                return value;
            }

            private void SkipWhiteSpace()
            {
                while (_index < _json.Length && char.IsWhiteSpace(_json[_index])) _index++;
            }

            private bool Peek(char c)
            {
                return _index < _json.Length && _json[_index] == c;
            }

            private void Expect(char c)
            {
                SkipWhiteSpace();
                if (_index >= _json.Length || _json[_index] != c)
                {
                    throw new FormatException("Expected '" + c + "' at JSON index " + _index.ToString(CultureInfo.InvariantCulture));
                }
                _index++;
            }
        }
    }

    internal static class ControlExtensions
    {
        public static void ToolTipTextSafe(this Control control, string text)
        {
            try
            {
                ToolTip tt = new ToolTip();
                tt.SetToolTip(control, text);
            }
            catch { }
        }
    }
}
