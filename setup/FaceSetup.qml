import QtQuick
import QtQuick.Effects
import Qt.labs.folderlistmodel
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pam
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Face Unlock settings, summoned from the Omarchy menu
// (omarchy-shell shell summon facecam.lock '{}'). Everything runs through the
// face daemon's socket: it owns the camera and keeps the models warm, so
// enrollment uses the exact pipeline that unlocks the screen.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false

  readonly property string home: Quickshell.env("HOME")
  readonly property string userName: Quickshell.env("USER") || Quickshell.env("LOGNAME")
  readonly property string runtimeDir: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/facecam-lock"
  readonly property string bootMarker: "/etc/facecam-lock/boot-unlock.conf"
  readonly property int authWindowMs: 5 * 60 * 1000

  // auth | home | name | enroll | photo | remove
  property string page: "auth"
  property string startAction: ""
  property double authedAt: 0
  property bool authenticating: false
  property string authError: ""
  property string pendingPassword: ""

  property var people: []
  property var settings: ({})
  property bool loaded: false
  property string notice: ""
  property bool noticeError: false
  property int avatarVersion: 0
  property bool bootUnlockConfigured: false

  // Whoever the current page acts on; nameMode is "add" or "rename".
  property string targetId: ""
  property string targetName: ""
  property string nameMode: "add"

  property string enrollState: ""
  property string enrollPrompt: ""
  property string enrollHint: ""
  property string enrollMessage: ""
  property int enrollCount: 0
  property int enrollTarget: 13
  property int previewTick: 0
  property var faceBox: null
  property var pending: []
  property bool daemonStartRequested: false

  readonly property bool enrolling: enrollState === "starting" || enrollState === "capturing" || enrollState === "saving"
  readonly property var owner: findPerson("owner")

  readonly property color fg: Color.menu.text
  readonly property color muted: Util.alpha(Color.menu.text, 0.62)
  readonly property color faint: Util.alpha(Color.menu.text, 0.1)
  readonly property color accent: Color.accent
  readonly property color errorColor: Color.urgent
  readonly property int cornerRadius: Style.cornerRadius
  readonly property int pad: Style.spacing.panelPadding + Style.space(6)
  readonly property int cardWidth: Math.min(Style.space(540), panel.width - Style.gapsOut * 2)

  function findPerson(id) {
    for (var i = 0; i < people.length; i++) if (people[i].id === id) return people[i]
    return null
  }

  function open(payloadJson) {
    var payload = ({})
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }
    root.startAction = payload.action || ""
    root.opened = true
    root.notice = ""
    root.authError = ""
    root.enrollState = ""
    bootMarkerView.reload()
    send({ cmd: "people" })
    if (Date.now() - root.authedAt < root.authWindowMs) enterHome()
    else {
      root.page = "auth"
      Qt.callLater(function() { passwordField.text = ""; passwordField.forceActiveFocus() })
    }
  }

  function close() {
    if (root.enrolling) send({ cmd: "enroll_stop" })
    root.opened = false
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "facecam.lock")
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function back() {
    if (root.page === "auth" || root.page === "home") { root.dismiss(); return }
    if (root.page === "enroll" && root.enrolling) send({ cmd: "enroll_stop" })
    enterHome()
  }

  function enterHome() {
    root.page = "home"
    root.enrollState = ""
    focusCatcher.forceActiveFocus()
    var action = root.startAction
    root.startAction = ""
    if (action === "add") startAdd()
    else if (action === "retrain") Qt.callLater(function() { startEnroll("owner", root.owner ? root.owner.name : "") })
  }

  // ------------------------------------------------------------------ socket
  function send(obj) {
    var line = JSON.stringify(obj)
    if (sock.connected) {
      sock.write(line + "\n")
      sock.flush()
      return
    }
    root.pending = root.pending.concat([line])
    connectRetry.tries = 0
    sock.connected = false
    sock.connected = true
  }

  function handleLine(line) {
    var msg
    try { msg = JSON.parse(String(line || "")) } catch (e) { return }
    if (msg.t === "people") {
      root.people = msg.people || []
      root.settings = msg.settings || ({})
      root.loaded = true
      root.avatarVersion += 1
    } else if (msg.t === "frame") {
      if (root.page !== "enroll") return
      root.faceBox = msg.face || null
      root.previewTick = msg.n
      camera.loadNextFrame()
    } else if (msg.t === "enroll") {
      if (root.page !== "enroll") return
      root.enrollState = msg.state
      root.enrollMessage = msg.message || ""
      if (msg.state === "capturing") root.enrollPrompt = msg.message || ""
      root.enrollHint = msg.hint || ""
      if (msg.count !== undefined) root.enrollCount = msg.count
      if (msg.target) root.enrollTarget = msg.target
    } else if (msg.t === "result") {
      root.notice = msg.message || ""
      root.noticeError = !msg.ok
      if (msg.cmd === "enroll" && !msg.ok) root.enrollState = "error"
      if (msg.ok && (msg.cmd === "photo" || msg.cmd === "remove" || msg.cmd === "rename")) enterHome()
    }
  }

  Socket {
    id: sock
    path: root.runtimeDir + "/ctl.sock"
    connected: false
    parser: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }
    onConnectionStateChanged: {
      if (!connected) return
      connectRetry.stop()
      var queued = root.pending
      root.pending = []
      for (var i = 0; i < queued.length; i++) write(queued[i] + "\n")
      flush()
    }
    onError: function(error) {
      if (root.pending.length === 0) return
      if (!root.daemonStartRequested) {
        root.daemonStartRequested = true
        daemonStart.running = true
      }
      connectRetry.restart()
    }
  }

  Timer {
    id: connectRetry
    interval: 150
    property int tries: 0
    onTriggered: {
      if (root.pending.length === 0) return
      tries += 1
      if (tries > 60) {
        root.pending = []
        root.notice = "The face daemon isn't running (systemctl --user status facecam-lock)"
        root.noticeError = true
        return
      }
      sock.connected = false
      sock.connected = true
    }
  }

  Process {
    id: daemonStart
    command: ["systemctl", "--user", "start", "facecam-lock.service"]
    onExited: root.daemonStartRequested = false
  }

  FileView {
    id: bootMarkerView
    path: root.bootMarker
    printErrors: false
    onLoaded: root.bootUnlockConfigured = true
    onLoadFailed: root.bootUnlockConfigured = false
  }

  // ------------------------------------------------------------------ auth
  function submitAuth(password) {
    if (root.authenticating || !password) return
    root.pendingPassword = password
    root.authError = ""
    root.authenticating = true
    if (!pam.start()) {
      root.authenticating = false
      root.authError = "Couldn't start authentication"
      return
    }
    Qt.callLater(respondAuth)
  }

  function respondAuth() {
    if (!root.authenticating || !pam.active || !pam.responseRequired) return
    pam.respond(root.pendingPassword)
  }

  PamContext {
    id: pam
    config: "omarchy-lock-password"
    user: root.userName
    onResponseRequiredChanged: root.respondAuth()
    onPamMessage: root.respondAuth()
    onCompleted: function(result) {
      root.authenticating = false
      root.pendingPassword = ""
      passwordField.text = ""
      if (result === PamResult.Success) {
        root.authedAt = Date.now()
        root.enterHome()
      } else {
        root.authError = "That password didn't work"
        passwordField.forceActiveFocus()
      }
    }
    onError: function(error) {
      root.authenticating = false
      root.pendingPassword = ""
      root.authError = "Authentication failed"
    }
  }

  // ------------------------------------------------------------------ actions
  function startAdd() {
    root.nameMode = "add"
    root.targetId = ""
    root.page = "name"
    Qt.callLater(function() { nameField.text = ""; nameField.forceActiveFocus() })
  }

  function startRename(person) {
    root.nameMode = "rename"
    root.targetId = person.id
    root.targetName = person.name
    root.page = "name"
    Qt.callLater(function() { nameField.text = person.name; nameField.selectAll(); nameField.forceActiveFocus() })
  }

  function submitName() {
    var name = nameField.text.trim()
    if (!name) return
    if (root.nameMode === "rename") send({ cmd: "rename", id: root.targetId, name: name })
    else startEnroll("", name)
  }

  function startEnroll(id, name) {
    root.targetId = id
    root.targetName = name
    root.enrollState = "starting"
    root.enrollMessage = "Starting camera…"
    root.enrollPrompt = ""
    root.enrollHint = ""
    root.enrollCount = 0
    root.faceBox = null
    camera.reset()
    root.notice = ""
    root.page = "enroll"
    focusCatcher.forceActiveFocus()
    send({ cmd: "enroll", id: id, name: name })
  }

  function startPhoto(person) {
    root.targetId = person.id
    root.targetName = person.name
    root.page = "photo"
    focusCatcher.forceActiveFocus()
  }

  function setSetting(key, value) {
    var next = ({})
    for (var k in root.settings) next[k] = root.settings[k]
    next[key] = value
    root.settings = next
    send({ cmd: "set", key: key, value: value })
  }

  function fileUrl(path, version) {
    if (!path) return ""
    return "file://" + String(path).split("/").map(encodeURIComponent).join("/") + "?v=" + version
  }

  // ------------------------------------------------------------------ pieces
  component SectionLabel: Text {
    color: root.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    font.capitalization: Font.AllUppercase
    font.letterSpacing: 1.2
  }

  component Avatar: Item {
    id: avatar
    property string path: ""
    property string name: ""
    readonly property bool hasImage: photo.status === Image.Ready
    readonly property real r: Style.cornerRadius > 0 ? width / 2 : Math.round(width * 0.12)

    Rectangle {
      anchors.fill: parent
      radius: avatar.r
      color: Util.alpha(root.accent, 0.16)
      border.width: 1
      border.color: Util.alpha(root.accent, 0.35)
      visible: !avatar.hasImage
      Text {
        anchors.centerIn: parent
        text: (avatar.name || "?").charAt(0).toUpperCase()
        color: root.accent
        font.family: Style.font.family
        font.pixelSize: Math.round(avatar.height * 0.42)
        font.weight: Font.DemiBold
      }
    }
    Rectangle {
      id: mask
      anchors.fill: parent
      radius: avatar.r
      visible: false
      layer.enabled: true
    }
    Image {
      id: photo
      anchors.fill: parent
      source: root.fileUrl(avatar.path, root.avatarVersion)
      fillMode: Image.PreserveAspectCrop
      asynchronous: true
      cache: false
      sourceSize.width: avatar.width * 2
      sourceSize.height: avatar.height * 2
      visible: false
    }
    MultiEffect {
      anchors.fill: parent
      source: photo
      visible: avatar.hasImage
      autoPaddingEnabled: false
      maskEnabled: true
      maskSource: mask
      maskThresholdMin: 0.5
      maskSpreadAtMin: 1.0
    }
  }

  component SettingRow: Item {
    id: row
    property string title: ""
    property string detail: ""
    property bool checked: false
    signal toggled()
    width: parent ? parent.width : 0
    implicitHeight: Math.max(texts.implicitHeight, toggleSwitch.implicitHeight)
    Column {
      id: texts
      anchors.left: parent.left
      anchors.right: toggleSwitch.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)
      Text {
        width: parent.width
        text: row.title
        color: root.fg
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
      Text {
        width: parent.width
        visible: text !== ""
        text: row.detail
        wrapMode: Text.WordWrap
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }
    ToggleSwitch {
      id: toggleSwitch
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      checked: row.checked
      foreground: root.fg
      accent: root.accent
      onToggled: row.toggled()
    }
  }

  // ------------------------------------------------------------------ window
  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "facecam-setup"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: if (!root.enrolling) root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: Math.min(content.implicitHeight + root.pad * 2, panel.height - Style.gapsOut * 2)
      anchors.centerIn: parent
      radius: root.cornerRadius
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      clip: true

      Behavior on height { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

      MouseArea { anchors.fill: parent; onClicked: focusCatcher.forceActiveFocus() }

      Item {
        id: focusCatcher
        anchors.fill: parent
        focus: true
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            root.back()
            event.accepted = true
          }
        }
      }

      Column {
        id: content
        x: root.pad
        y: root.pad
        width: card.width - root.pad * 2
        spacing: Style.space(18)

        // ---------------------------------------------------------- header
        Item {
          width: parent.width
          height: headerRow.implicitHeight

          Row {
            id: headerRow
            spacing: Style.space(12)
            Rectangle {
              width: Style.space(38)
              height: width
              radius: Style.cornerRadius > 0 ? width / 2 : 0
              color: Util.alpha(root.accent, 0.14)
              anchors.verticalCenter: parent.verticalCenter
              Text {
                anchors.centerIn: parent
                text: "󰄀"
                color: root.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.display * 0.8
              }
            }
            Column {
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)
              Text {
                text: root.page === "enroll" ? (root.targetId === "owner" ? "Train your face" : "Enroll " + root.targetName)
                  : root.page === "photo" ? "Welcome photo for " + root.targetName
                  : root.page === "name" ? (root.nameMode === "rename" ? "Rename " + root.targetName : "Add a person")
                  : root.page === "remove" ? "Remove " + root.targetName
                  : "Face Unlock"
                color: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.heading
                font.weight: Font.DemiBold
              }
              Text {
                text: root.page === "auth" ? "Confirm it's you to change face unlock"
                  : root.page === "enroll" ? "Follow the prompts; it takes about 15 seconds"
                  : root.page === "photo" ? "Shown on the lock screen only when this face is recognised"
                  : "Unlock Omarchy by looking at the camera"
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          Button {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.page === "home" || root.page === "auth" ? "󰅖" : "󰁍"
            tooltipText: root.page === "home" || root.page === "auth" ? "Close" : "Back"
            foreground: root.fg
            onClicked: root.back()
          }
        }

        // ---------------------------------------------------------- auth
        Column {
          visible: root.page === "auth"
          width: parent.width
          spacing: Style.space(10)

          TextField {
            id: passwordField
            width: parent.width
            password: true
            placeholderText: root.authenticating ? "Checking…" : "Password for " + root.userName
            enabled: !root.authenticating
            foreground: root.fg
            accent: root.accent
            onAccepted: root.submitAuth(text)
            Keys.onEscapePressed: root.dismiss()
          }
          Text {
            visible: root.authError !== ""
            text: root.authError
            color: root.errorColor
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
          Button {
            anchors.right: parent.right
            text: root.authenticating ? "Checking…" : "Continue"
            iconText: "󰌾"
            bordered: true
            foreground: root.fg
            accent: root.accent
            onClicked: root.submitAuth(passwordField.text)
          }
        }

        // ---------------------------------------------------------- home
        Column {
          visible: root.page === "home"
          width: parent.width
          spacing: Style.space(18)

          // First run: nobody enrolled yet.
          BorderSurface {
            visible: root.loaded && !root.owner
            width: parent.width
            height: firstRun.implicitHeight + Style.space(36)
            radius: root.cornerRadius
            color: Util.alpha(root.accent, 0.08)
            borderSpec: Border.flat(Util.alpha(root.accent, 0.3), 1)
            Column {
              id: firstRun
              anchors.centerIn: parent
              width: parent.width - Style.space(36)
              spacing: Style.space(10)
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "Face unlock isn't set up yet"
                color: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.title
              }
              Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Enroll my face"
                iconText: "󰄀"
                bordered: true
                foreground: root.fg
                accent: root.accent
                onClicked: root.startEnroll("owner", "")
              }
            }
          }

          Column {
            visible: root.people.length > 0
            width: parent.width
            spacing: Style.space(8)

            SectionLabel { text: "Who can unlock" }

            Repeater {
              model: root.people
              delegate: BorderSurface {
                required property var modelData
                width: parent.width
                height: Style.space(64)
                radius: root.cornerRadius
                color: personHover.hovered ? Util.alpha(root.fg, 0.05) : "transparent"
                borderSpec: Border.flat(root.faint, 1)

                HoverHandler { id: personHover }

                Avatar {
                  id: personAvatar
                  x: Style.space(12)
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(42)
                  height: width
                  path: modelData.avatar
                  name: modelData.name
                }

                Column {
                  anchors.left: personAvatar.right
                  anchors.leftMargin: Style.space(12)
                  anchors.right: actions.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)
                  Row {
                    spacing: Style.space(8)
                    Text {
                      text: modelData.name
                      color: root.fg
                      font.family: Style.font.family
                      font.pixelSize: Style.font.title
                      font.weight: Font.Medium
                    }
                    Rectangle {
                      visible: modelData.owner
                      anchors.verticalCenter: parent.verticalCenter
                      width: youLabel.implicitWidth + Style.space(12)
                      height: youLabel.implicitHeight + Style.space(4)
                      radius: Style.cornerRadius > 0 ? height / 2 : 0
                      color: Util.alpha(root.accent, 0.16)
                      Text {
                        id: youLabel
                        anchors.centerIn: parent
                        text: "You"
                        color: root.accent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                      }
                    }
                  }
                  Text {
                    text: modelData.samples + " face samples" + (modelData.avatar ? " · welcome photo set" : "")
                    color: root.muted
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }
                }

                Row {
                  id: actions
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)
                  Button {
                    iconText: "󰑐"
                    tooltipText: "Retrain face"
                    foreground: root.fg
                    accent: root.accent
                    onClicked: root.startEnroll(modelData.id, modelData.name)
                  }
                  Button {
                    iconText: "󰋩"
                    tooltipText: "Welcome photo"
                    foreground: root.fg
                    accent: root.accent
                    onClicked: root.startPhoto(modelData)
                  }
                  Button {
                    iconText: "󰏫"
                    tooltipText: "Rename"
                    foreground: root.fg
                    accent: root.accent
                    onClicked: root.startRename(modelData)
                  }
                  Button {
                    iconText: "󰆴"
                    tooltipText: "Remove"
                    foreground: root.fg
                    accent: root.errorColor
                    onClicked: {
                      root.targetId = modelData.id
                      root.targetName = modelData.name
                      root.page = "remove"
                    }
                  }
                }
              }
            }

            Button {
              visible: !!root.owner
              text: "Add someone else"
              iconText: "󰐕"
              foreground: root.fg
              accent: root.accent
              onClicked: root.startAdd()
            }
          }

          Rectangle { width: parent.width; height: 1; color: root.faint }

          Column {
            width: parent.width
            spacing: Style.space(14)

            SectionLabel { text: "Settings" }

            SettingRow {
              title: "Face unlock"
              detail: "Look at the camera to unlock; your password always works too"
              checked: root.settings.enable_face_unlock !== false
              onToggled: root.setSetting("enable_face_unlock", !checked)
            }
            SettingRow {
              title: "Face lock at startup"
              detail: root.bootUnlockConfigured
                ? "Your disk unlocks with the TPM; this lock protects the session after boot"
                : "Locks right after login. Turn on with disk unlock at boot, or you'll get both prompts"
              checked: root.settings.lock_on_startup === true
              onToggled: root.setSetting("lock_on_startup", !checked)
            }

            Item {
              width: parent.width
              height: Math.max(bootTexts.implicitHeight, bootButton.implicitHeight)
              Column {
                id: bootTexts
                anchors.left: parent.left
                anchors.right: bootButton.left
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Text {
                  text: "Skip the disk password at boot"
                  color: root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
                Text {
                  width: parent.width
                  wrapMode: Text.WordWrap
                  text: root.bootUnlockConfigured
                    ? "On: the TPM unlocks the disk, then your face unlocks the session"
                    : "The TPM unlocks the disk so you only see the face lock. Needs Secure Boot"
                  color: root.muted
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                }
              }
              Button {
                id: bootButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.bootUnlockConfigured ? "Manage" : "Set up"
                bordered: true
                foreground: root.fg
                accent: root.accent
                onClicked: {
                  root.dismiss()
                  Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
                    "facecam-lock boot " + (root.bootUnlockConfigured ? "status" : "enable")])
                }
              }
            }
          }

          Item {
            width: parent.width
            height: testButton.implicitHeight
            Text {
              anchors.left: parent.left
              anchors.right: testButton.left
              anchors.rightMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              text: root.notice
              elide: Text.ElideRight
              color: root.noticeError ? root.errorColor : root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              id: testButton
              anchors.right: parent.right
              visible: !!root.owner
              text: "Try the lock screen"
              iconText: "󰌾"
              bordered: true
              foreground: root.fg
              accent: root.accent
              onClicked: {
                root.dismiss()
                Quickshell.execDetached(["omarchy-system-lock"])
              }
            }
          }
        }

        // ---------------------------------------------------------- name
        Column {
          visible: root.page === "name"
          width: parent.width
          spacing: Style.space(10)
          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.nameMode === "rename" ? "The lock screen says \"Welcome back\" followed by this name."
              : "They'll sit in front of the camera next. The lock screen greets them by this name."
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
          TextField {
            id: nameField
            width: parent.width
            placeholderText: "Name, e.g. Alex"
            maximumLength: 40
            foreground: root.fg
            accent: root.accent
            onAccepted: root.submitName()
            Keys.onEscapePressed: root.back()
          }
          Button {
            anchors.right: parent.right
            text: root.nameMode === "rename" ? "Save" : "Start camera"
            iconText: root.nameMode === "rename" ? "󰄬" : "󰄀"
            bordered: true
            foreground: root.fg
            accent: root.accent
            onClicked: root.submitName()
          }
        }

        // ---------------------------------------------------------- enroll
        Column {
          visible: root.page === "enroll"
          width: parent.width
          spacing: Style.space(16)

          Item {
            id: camera
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(Style.space(300), parent.width)
            height: width
            readonly property int radius: Style.cornerRadius > 0 ? Math.min(Math.round(width * 0.18), Style.cornerRadius * 3) : Math.round(width * 0.04)
            property int shown: 0
            property bool ready: false
            readonly property string url: "file://" + root.runtimeDir + "/preview.jpg"

            function reset() {
              frameA.source = ""
              frameB.source = ""
              ready = false
            }
            function loadNextFrame() {
              var back = shown === 0 ? frameB : frameA
              if (back.status === Image.Loading) return
              back.source = url + "?t=" + root.previewTick
            }
            function swap(index) {
              if (index !== shown) shown = index
              ready = true
            }

            Rectangle {
              anchors.fill: parent
              radius: camera.radius
              color: Util.alpha(Color.background, 0.9)
              border.width: 1
              border.color: root.faint
            }

            Rectangle {
              id: cameraMask
              anchors.fill: parent
              radius: camera.radius
              visible: false
              layer.enabled: true
            }

            Item {
              anchors.fill: parent
              layer.enabled: true
              layer.effect: MultiEffect {
                autoPaddingEnabled: false
                maskEnabled: true
                maskSource: cameraMask
                maskThresholdMin: 0.5
                maskSpreadAtMin: 1.0
              }

              Image {
                id: frameA
                anchors.fill: parent
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: false
                mirror: true
                visible: camera.ready && camera.shown === 0
                onStatusChanged: if (status === Image.Ready) camera.swap(0)
              }
              Image {
                id: frameB
                anchors.fill: parent
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: false
                mirror: true
                visible: camera.ready && camera.shown === 1
                onStatusChanged: if (status === Image.Ready) camera.swap(1)
              }

              // The face the daemon is tracking. The preview is cropped to a
              // square and mirrored, so map the box through the same transform.
              Rectangle {
                id: faceRect
                readonly property var box: root.faceBox
                readonly property real srcW: frameA.implicitWidth || frameB.implicitWidth || 1
                readonly property real srcH: frameA.implicitHeight || frameB.implicitHeight || 1
                readonly property real k: Math.max(camera.width / srcW, camera.height / srcH)
                readonly property real offX: (camera.width - srcW * k) / 2
                readonly property real offY: (camera.height - srcH * k) / 2
                visible: camera.ready && box !== null && root.enrollState === "capturing"
                x: box ? offX + (1 - box[0] - box[2]) * srcW * k : 0
                y: box ? offY + box[1] * srcH * k : 0
                width: box ? box[2] * srcW * k : 0
                height: box ? box[3] * srcH * k : 0
                radius: Style.cornerRadius > 0 ? Math.round(width * 0.12) : 0
                color: "transparent"
                border.width: 2
                border.color: root.enrollHint ? root.errorColor : root.accent
                Behavior on x { NumberAnimation { duration: 90 } }
                Behavior on y { NumberAnimation { duration: 90 } }
                Behavior on width { NumberAnimation { duration: 90 } }
                Behavior on height { NumberAnimation { duration: 90 } }
              }

              Rectangle {
                anchors.fill: parent
                color: Color.background
                opacity: root.enrollState === "done" || root.enrollState === "error" || root.enrollState === "saving" ? 0.72 : 0
                Behavior on opacity { NumberAnimation { duration: 220 } }
              }
            }

            BorderSurface {
              anchors.fill: parent
              radius: camera.radius
              color: "transparent"
              borderSpec: root.enrollState === "done" ? Border.flat(root.accent, 3)
                : root.enrollState === "error" ? Border.flat(root.errorColor, 2)
                : Border.hyprlandActiveSpec(root.accent, 2)
            }

            Text {
              anchors.centerIn: parent
              visible: !camera.ready || root.enrollState === "done" || root.enrollState === "error" || root.enrollState === "saving"
              text: root.enrollState === "done" ? "󰄬" : root.enrollState === "error" ? "󰅙" : "󰄀"
              color: root.enrollState === "error" ? root.errorColor : root.accent
              font.family: Style.font.family
              font.pixelSize: Math.round(camera.width * 0.2)
              SequentialAnimation on opacity {
                running: root.opened && (root.enrollState === "starting" || root.enrollState === "saving")
                loops: Animation.Infinite
                NumberAnimation { from: 1; to: 0.35; duration: 800; easing.type: Easing.InOutSine }
                NumberAnimation { from: 0.35; to: 1; duration: 800; easing.type: Easing.InOutSine }
              }
            }
          }

          // One segment per capture.
          Row {
            id: progress
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(4)
            readonly property real segment: (camera.width - spacing * (root.enrollTarget - 1)) / Math.max(1, root.enrollTarget)
            Repeater {
              model: root.enrollTarget
              Rectangle {
                width: progress.segment
                height: Style.space(5)
                radius: Style.cornerRadius > 0 ? height / 2 : 0
                color: index < root.enrollCount ? root.accent : root.faint
                Behavior on color { ColorAnimation { duration: 160 } }
              }
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(4)
            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              text: root.enrollState === "capturing" ? root.enrollPrompt : root.enrollMessage
              color: root.enrollState === "error" ? root.errorColor : (root.enrollState === "done" ? root.accent : root.fg)
              font.family: Style.font.family
              font.pixelSize: Style.font.heading
              font.weight: Font.Medium
            }
            Text {
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
              text: root.enrollHint !== "" ? root.enrollHint
                : root.enrollState === "capturing" ? root.enrollCount + " of " + root.enrollTarget + " captures"
                : root.enrollState === "done" ? "Lock your screen to try it"
                : ""
              color: root.enrollHint !== "" ? root.errorColor : root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(8)
            Button {
              visible: root.enrolling
              text: "Cancel"
              bordered: true
              foreground: root.fg
              accent: root.accent
              onClicked: root.back()
            }
            Button {
              visible: root.enrollState === "error" || root.enrollState === "cancelled"
              text: "Try again"
              iconText: "󰑐"
              bordered: true
              foreground: root.fg
              accent: root.accent
              onClicked: root.startEnroll(root.targetId, root.targetName)
            }
            Button {
              visible: !root.enrolling
              text: root.enrollState === "done" ? "Done" : "Back"
              bordered: root.enrollState === "done"
              foreground: root.fg
              accent: root.accent
              onClicked: root.enterHome()
            }
          }
        }

        // ---------------------------------------------------------- photo
        Column {
          visible: root.page === "photo"
          width: parent.width
          spacing: Style.space(12)

          Row {
            spacing: Style.space(4)
            Repeater {
              model: [{ label: "Pictures", dir: root.home + "/Pictures" }, { label: "Downloads", dir: root.home + "/Downloads" }]
              Button {
                required property var modelData
                text: modelData.label
                selected: photoFolder.folder.toString() === "file://" + modelData.dir
                foreground: root.fg
                accent: root.accent
                onClicked: photoFolder.folder = "file://" + modelData.dir
              }
            }
          }

          FolderListModel {
            id: photoFolder
            folder: "file://" + root.home + "/Pictures"
            nameFilters: ["*.jpg", "*.jpeg", "*.png", "*.webp", "*.JPG", "*.JPEG", "*.PNG"]
            showDirs: false
            sortField: FolderListModel.Time
          }

          GridView {
            id: photoGrid
            width: parent.width
            height: Math.min(Style.space(300), Math.max(Style.space(90), Math.ceil(count / 5) * cellHeight))
            cellWidth: Math.floor(width / 5)
            cellHeight: cellWidth
            clip: true
            model: root.page === "photo" ? photoFolder : null
            delegate: Item {
              required property string filePath
              required property string fileName
              width: photoGrid.cellWidth
              height: photoGrid.cellHeight
              BorderSurface {
                anchors.fill: parent
                anchors.margins: Style.space(3)
                radius: root.cornerRadius
                color: Util.alpha(root.fg, 0.05)
                borderSpec: Border.flat(thumbHover.hovered ? root.accent : root.faint, thumbHover.hovered ? 2 : 1)
                clip: true
                Image {
                  anchors.fill: parent
                  anchors.margins: 2
                  source: "file://" + filePath
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  sourceSize.width: 192
                  sourceSize.height: 192
                }
                HoverHandler { id: thumbHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: root.send({ cmd: "photo", id: root.targetId, path: filePath }) }
              }
            }
            Text {
              anchors.centerIn: parent
              visible: photoGrid.count === 0
              text: "No photos in this folder"
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(8)
            TextField {
              id: photoPath
              width: parent.width - usePath.width - parent.spacing
              placeholderText: "…or a path, e.g. ~/Downloads/me.jpg"
              foreground: root.fg
              accent: root.accent
              onAccepted: if (text.trim()) root.send({ cmd: "photo", id: root.targetId, path: text.trim() })
              Keys.onEscapePressed: root.back()
            }
            Button {
              id: usePath
              text: "Use"
              bordered: true
              foreground: root.fg
              accent: root.accent
              onClicked: if (photoPath.text.trim()) root.send({ cmd: "photo", id: root.targetId, path: photoPath.text.trim() })
            }
          }

          Text {
            visible: root.notice !== ""
            text: root.notice
            color: root.noticeError ? root.errorColor : root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ---------------------------------------------------------- remove
        Column {
          visible: root.page === "remove"
          width: parent.width
          spacing: Style.space(14)
          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.targetId === "owner"
              ? "This deletes your face profile and photo. You'll unlock with your password until you enroll again."
              : root.targetName + " won't be able to unlock with their face anymore. Their photo is deleted too."
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
          Row {
            anchors.right: parent.right
            spacing: Style.space(8)
            Button {
              text: "Keep"
              foreground: root.fg
              accent: root.accent
              onClicked: root.enterHome()
            }
            Button {
              text: "Remove"
              iconText: "󰆴"
              bordered: true
              foreground: root.errorColor
              accent: root.errorColor
              onClicked: root.send({ cmd: "remove", id: root.targetId })
            }
          }
        }
      }
    }
  }
}
