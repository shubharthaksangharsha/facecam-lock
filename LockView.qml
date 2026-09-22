import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Ui

Item {
  id: root

  property string backgroundPath: ""
  property int backgroundVersion: 0
  property bool fingerprintConfigured: false
  property bool authenticatingPassword: false
  property string failureMessage: ""
  property int failedAttempts: 0
  property bool inputEnabled: true
  property bool loadBackground: true
  property string passwordText: ""
  property bool syncingPasswordText: false
  property string faceState: "idle"
  property string faceMessage: ""
  property string displayName: "Shubharthak"
  property string previewPath: ""
  property int previewTick: 0
  property bool passwordPanelOpen: false

  readonly property string placeholderText: "Enter Password"
  readonly property int fieldWidth: 381
  readonly property int fieldHeight: 67
  readonly property int outlineThickness: 3
  readonly property int fieldFontSize: Math.round(Style.font.heading * 1.125)
  readonly property int passwordDotFontSize: Math.round(Style.font.heading * 1.33)
  readonly property int passwordDotLetterSpacing: Math.round(Style.font.heading * 0.19)
  readonly property real fingerprintReserve: 0
  readonly property real passwordDotScale: dotMetrics.advanceWidth > 0
    ? Math.min(1, (passwordInput.width - 4) / dotMetrics.advanceWidth)
    : 1
  readonly property bool showPasswordCursor: inputEnabled && !authenticatingPassword && failureMessage.length === 0
  readonly property bool errorState: failureMessage.length > 0
  readonly property bool faceFailed: faceState === "failed" || faceState === "camera_error" || faceState === "no_profile"
  readonly property bool showPasswordField: passwordPanelOpen || faceFailed || authenticatingPassword || failureMessage.length > 0
  readonly property bool matched: faceState === "matched"
  readonly property var inputBorderSpec: errorState
    ? Border.surfaceSpec("lock", "border-error", Color.lock.borderError, root.outlineThickness, "border-alpha")
    : Border.surfaceSpec("lock", "border-active", Color.lock.borderActive, root.outlineThickness, "border-alpha")
  readonly property string statusLine: {
    if (matched) return displayName ? "Welcome back " + displayName : "Welcome back"
    if (faceFailed) return faceMessage || "Can't detect face"
    if (faceMessage.length > 0) return faceMessage
    return "Looking for your face…"
  }

  signal submitPassword(string password)
  signal passwordTextEdited(string password)
  signal clearFailureRequested()
  signal wakeRequested()
  signal passwordPanelRequested()

  function fileUrl(path) {
    if (!path) return ""
    var encoded = String(path).split("/").map(encodeURIComponent).join("/")
    return "file://" + encoded + "?v=" + backgroundVersion
  }

  function previewUrl() {
    if (!previewPath) return ""
    var encoded = String(previewPath).split("/").map(encodeURIComponent).join("/")
    return "file://" + encoded + "?t=" + previewTick
  }

  // Two Images swap roles: the hidden one decodes the next frame and only takes
  // over once Ready, so the preview never blanks between frames.
  property int shownBuffer: 0
  property bool frameReady: false

  function loadNextFrame() {
    var url = previewUrl()
    if (!url) return
    var back = shownBuffer === 0 ? previewB : previewA
    if (back.status === Image.Loading) return
    back.source = url
  }

  function onBufferReady(index) {
    if (index === shownBuffer) return
    shownBuffer = index
    frameReady = true
  }

  onPreviewTickChanged: loadNextFrame()
  onFaceStateChanged: {
    if (faceState === "idle") {
      previewA.source = ""
      previewB.source = ""
      frameReady = false
    }
  }

  function forcePasswordFocus() {
    if (showPasswordField) passwordInput.forceActiveFocus()
  }

  function clearPassword() {
    passwordTextEdited("")
  }

  function syncPasswordText() {
    if (passwordInput.text === passwordText) return
    syncingPasswordText = true
    passwordInput.text = passwordText
    syncingPasswordText = false
  }

  function openPassword() {
    passwordPanelRequested()
    Qt.callLater(forcePasswordFocus)
  }

  onPasswordTextChanged: syncPasswordText()
  onInputEnabledChanged: {
    if (inputEnabled && showPasswordField) Qt.callLater(forcePasswordFocus)
  }
  onShowPasswordFieldChanged: {
    if (showPasswordField && inputEnabled) Qt.callLater(forcePasswordFocus)
  }
  onFaceFailedChanged: {
    if (faceFailed) passwordPanelRequested()
  }
  Component.onCompleted: {
    syncPasswordText()
    clockTimer.restart()
  }

  TextMetrics {
    id: dotMetrics
    font.family: Style.font.family
    font.pixelSize: root.passwordDotFontSize
    font.letterSpacing: root.passwordDotLetterSpacing
    text: "●".repeat(passwordInput.text.length)
  }

  Rectangle {
    anchors.fill: parent
    color: Color.background

    Image {
      id: wallpaper
      anchors.fill: parent
      source: root.loadBackground ? root.fileUrl(root.backgroundPath) : ""
      fillMode: Image.PreserveAspectCrop
      asynchronous: true
      cache: false
      sourceSize.width: width
      sourceSize.height: height
    }

    MultiEffect {
      anchors.fill: wallpaper
      source: wallpaper
      autoPaddingEnabled: false
      blurEnabled: root.loadBackground && wallpaper.status === Image.Ready
      blur: 1.0
      blurMax: 128
      blurMultiplier: 1.25
      contrast: -0.08
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      onClicked: { root.wakeRequested(); if (root.showPasswordField) root.forcePasswordFocus() }
      onPositionChanged: root.wakeRequested()
    }

    Column {
      anchors.fill: parent
      anchors.topMargin: Math.round(parent.height * 0.07)
      anchors.bottomMargin: Math.round(Style.space(72))
      spacing: Style.space(14)
      clip: true

      Column {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(4)

        Text {
          id: clockLabel
          anchors.horizontalCenter: parent.horizontalCenter
          text: Qt.formatTime(new Date(), "hh:mm")
          color: Color.lock.text
          font.family: Style.font.family
          font.pixelSize: Math.round(Style.font.heading * 3.4)
          font.weight: Font.Light
        }

        Text {
          anchors.horizontalCenter: parent.horizontalCenter
          text: Qt.formatDate(new Date(), "dddd, MMMM d")
          color: Color.lock.placeholder
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Item {
        id: cameraFrame
        anchors.horizontalCenter: parent.horizontalCenter
        readonly property int boxSize: {
          var maxByHeight = Math.round(root.height * 0.34)
          var maxByWidth = Math.round(root.width * 0.42)
          var preferred = Style.space(220)
          return Math.max(Style.space(156), Math.min(preferred, maxByHeight, maxByWidth))
        }
        readonly property int boxRadius: Math.round(boxSize * 0.18)
        readonly property int innerPad: 3
        readonly property int innerSize: Math.max(1, boxSize - innerPad * 2)
        readonly property int innerRadius: Math.max(1, boxRadius - innerPad)
        width: boxSize
        height: boxSize
        clip: true

        Rectangle {
          anchors.fill: parent
          radius: cameraFrame.boxRadius
          color: "#05070c"
        }

        Rectangle {
          id: cameraMask
          width: cameraFrame.innerSize
          height: cameraFrame.innerSize
          radius: cameraFrame.innerRadius
          color: "#ffffff"
          visible: false
          layer.enabled: true
          layer.smooth: true
        }

        Item {
          id: cameraContent
          x: cameraFrame.innerPad
          y: cameraFrame.innerPad
          width: cameraFrame.innerSize
          height: cameraFrame.innerSize
          layer.enabled: true
          layer.smooth: true
          layer.effect: MultiEffect {
            autoPaddingEnabled: false
            maskEnabled: true
            maskSource: cameraMask
            maskThresholdMin: 0.5
            maskSpreadAtMin: 1.0
          }

          Image {
            id: previewA
            anchors.fill: parent
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            mirror: true
            visible: root.frameReady && root.shownBuffer === 0
            onStatusChanged: if (status === Image.Ready) root.onBufferReady(0)
          }

          Image {
            id: previewB
            anchors.fill: parent
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            mirror: true
            visible: root.frameReady && root.shownBuffer === 1
            onStatusChanged: if (status === Image.Ready) root.onBufferReady(1)
          }

          Rectangle {
            id: scanLine
            x: cameraFrame.innerRadius
            width: Math.max(1, cameraFrame.innerSize - cameraFrame.innerRadius * 2)
            height: 2
            y: cameraFrame.innerRadius
            visible: !root.matched && !root.faceFailed
            color: Color.lock.borderActive
            opacity: 0.8

            SequentialAnimation on y {
              running: root.loadBackground && !root.matched && !root.faceFailed
              loops: Animation.Infinite
              NumberAnimation {
                to: Math.max(cameraFrame.innerRadius, cameraFrame.innerSize - cameraFrame.innerRadius)
                duration: 1600
                easing.type: Easing.InOutSine
              }
              NumberAnimation {
                to: cameraFrame.innerRadius
                duration: 1600
                easing.type: Easing.InOutSine
              }
            }
          }
        }

        Rectangle {
          anchors.fill: parent
          radius: cameraFrame.boxRadius
          color: "transparent"
          border.width: 3
          border.color: root.matched ? "#10b981" : (root.faceFailed ? Color.lock.borderError : Color.lock.borderActive)
        }

        Text {
          anchors.centerIn: parent
          visible: !root.frameReady
          text: "󰄀"
          color: Color.lock.placeholder
          font.family: Style.font.family
          font.pixelSize: Math.round(Style.font.heading * 2)
        }

        Text {
          anchors.centerIn: parent
          visible: root.matched
          text: "✓"
          color: "#10b981"
          font.pixelSize: Math.round(Style.font.heading * 2.6)
        }
      }

      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        width: parent.width - Style.space(40)
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        text: root.statusLine
        color: root.matched ? "#34d399" : (root.faceFailed ? Color.lock.textError : Color.lock.text)
        font.family: Style.font.family
        font.pixelSize: root.matched ? Math.round(Style.font.heading * 1.15) : Style.font.body
        font.weight: root.matched ? Font.DemiBold : Font.Normal
      }

      Item {
        width: parent.width
        height: root.showPasswordField ? inputField.height : 0
        visible: root.showPasswordField

        BorderSurface {
          id: inputField
          width: root.fieldWidth
          height: root.fieldHeight
          anchors.horizontalCenter: parent.horizontalCenter
          color: Color.lock.background
          borderSpec: root.inputBorderSpec
          radius: Style.cornerRadius
          clip: true

          TextInput {
            id: passwordInput
            anchors.fill: parent
            anchors.topMargin: inputField.borderTop
            anchors.rightMargin: inputField.borderRight + 18
            anchors.bottomMargin: inputField.borderBottom
            anchors.leftMargin: inputField.borderLeft + 18
            verticalAlignment: TextInput.AlignVCenter
            horizontalAlignment: TextInput.AlignHCenter
            activeFocusOnPress: true
            clip: true
            enabled: root.inputEnabled && !root.authenticatingPassword
            readOnly: root.authenticatingPassword
            echoMode: TextInput.Password
            passwordCharacter: "\u25CF"
            passwordMaskDelay: 0
            color: Color.lock.text
            selectionColor: Color.lock.selection
            selectedTextColor: Color.lock.text
            font.family: Style.font.family
            font.pixelSize: text.length > 0 ? Math.max(1, Math.floor(root.passwordDotFontSize * root.passwordDotScale)) : root.fieldFontSize
            font.letterSpacing: text.length > 0 ? root.passwordDotLetterSpacing * root.passwordDotScale : 0
            cursorVisible: activeFocus && root.showPasswordCursor && text.length > 0
            cursorDelegate: Rectangle {
              width: 2
              color: Color.lock.text
              visible: passwordInput.cursorVisible
            }

            onTextChanged: {
              if (!root.syncingPasswordText) root.passwordTextEdited(text)
              if (text.length > 0) root.wakeRequested()
              if (text.length > 0 && root.failureMessage.length > 0) root.clearFailureRequested()
            }

            onAccepted: {
              var submitted = root.passwordText
              root.passwordTextEdited("")
              if (submitted.length > 0) root.submitPassword(submitted)
            }

            Keys.onPressed: function(event) {
              root.wakeRequested()
              if (event.key === Qt.Key_Escape || (event.modifiers & Qt.ControlModifier && event.key === Qt.Key_U)) {
                root.passwordTextEdited("")
                event.accepted = true
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            anchors.fill: passwordInput
            text: root.authenticatingPassword ? "Checking…" : (root.failureMessage.length > 0 ? root.failureMessage : root.placeholderText)
            visible: passwordInput.text.length === 0
            color: root.authenticatingPassword ? Color.lock.text : (root.failureMessage.length > 0 ? Color.lock.textError : Color.lock.placeholder)
            font.family: Style.font.family
            font.pixelSize: root.fieldFontSize
            font.italic: !root.authenticatingPassword && root.failureMessage.length > 0
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
          }
        }
      }

      Item { width: 1; height: Style.space(8) }
    }

    Button {
      id: passwordButton
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Math.round(Style.space(28))
      text: root.showPasswordField ? "Password field ready" : "Type password"
      bordered: true
      enabled: root.inputEnabled
      onClicked: {
        root.wakeRequested()
        root.openPassword()
      }
    }
  }

  Timer {
    id: clockTimer
    interval: 1000
    repeat: true
    running: true
    onTriggered: {
      clockLabel.text = Qt.formatTime(new Date(), "hh:mm")
    }
  }
}
