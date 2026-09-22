import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
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
  property string displayName: ""
  property string previewPath: ""
  property int previewTick: 0
  property string avatarPath: ""
  property int faceAttempt: 1
  property int faceMaxAttempts: 3
  property bool passwordPanelOpen: false

  readonly property string placeholderText: "Enter Password"
  readonly property int fieldWidth: 381
  readonly property int fieldHeight: 67
  readonly property int outlineThickness: 3
  readonly property int fieldFontSize: Math.round(Style.font.heading * 1.125)
  readonly property int passwordDotFontSize: Math.round(Style.font.heading * 1.33)
  readonly property int passwordDotLetterSpacing: Math.round(Style.font.heading * 0.19)
  readonly property real passwordDotScale: dotMetrics.advanceWidth > 0
    ? Math.min(1, (passwordInput.width - 4) / dotMetrics.advanceWidth)
    : 1
  readonly property bool showPasswordCursor: inputEnabled && !authenticatingPassword && failureMessage.length === 0
  readonly property bool errorState: failureMessage.length > 0
  readonly property bool faceFailed: faceState === "failed" || faceState === "camera_error" || faceState === "no_profile"
  readonly property bool showPasswordField: passwordPanelOpen || faceFailed || authenticatingPassword || failureMessage.length > 0
  readonly property bool matched: faceState === "matched"
  readonly property bool scanning: faceState === "scanning" || faceState === "waiting"
  readonly property bool animate: loadBackground
  readonly property bool hasAvatar: avatarImage.status === Image.Ready
  readonly property color accentColor: Color.lock.borderActive
  readonly property color softError: Util.alpha(Color.lock.borderError, 0.8)
  readonly property var inputBorderSpec: errorState
    ? Border.surfaceSpec("lock", "border-error", Color.lock.borderError, root.outlineThickness, "border-alpha")
    : Border.surfaceSpec("lock", "border-active", Color.lock.borderActive, root.outlineThickness, "border-alpha")

  readonly property string statusLine: {
    if (matched) return displayName ? "Welcome back " + displayName : "Welcome back"
    if (faceState === "no_profile") return "Face unlock isn't set up"
    if (faceState === "camera_error") return "Camera unavailable"
    if (faceFailed) return "Face not recognised"
    if (faceState === "waiting") return "Open the lid to scan"
    return "Looking for your face"
  }
  readonly property string statusDetail: {
    if (matched) return "Unlocking…"
    if (faceState === "no_profile") return "Open FaceCam Lock to enroll, or type your password"
    if (faceFailed) return "Tap the camera to try again, or type your password"
    if (scanning && (!frameReady || awaitingFrame)) return "Starting camera…"
    if (scanning && faceAttempt > 1) return "Attempt " + faceAttempt + " of " + faceMaxAttempts
    return "Hold still and look at the screen"
  }

  signal submitPassword(string password)
  signal passwordTextEdited(string password)
  signal clearFailureRequested()
  signal wakeRequested()
  signal passwordPanelRequested()
  signal retryRequested()

  // Four rounded L-shaped corners: the viewfinder and the small "no face" icon.
  component Corners: Item {
    id: corners
    property real length: 18
    property real thickness: 3
    property color color: "white"
    Repeater {
      model: 4
      Item {
        readonly property bool isRight: index % 2 === 1
        readonly property bool isBottom: index >= 2
        x: isRight ? corners.width - width : 0
        y: isBottom ? corners.height - height : 0
        width: corners.length
        height: corners.length
        Rectangle {
          width: parent.width; height: corners.thickness; radius: height / 2
          y: parent.isBottom ? parent.height - height : 0
          color: corners.color
        }
        Rectangle {
          width: corners.thickness; height: parent.height; radius: width / 2
          x: parent.isRight ? parent.width - width : 0
          color: corners.color
        }
      }
    }
  }

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
  // True from the moment a scan (re)starts until its first fresh frame lands.
  property bool awaitingFrame: false
  property bool wasScanning: false

  function loadNextFrame() {
    var url = previewUrl()
    if (!url) return
    var back = shownBuffer === 0 ? previewB : previewA
    if (back.status === Image.Loading) return
    back.source = url
  }

  function onBufferReady(index) {
    awaitingFrame = false
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
    if (scanning && !wasScanning) awaitingFrame = true
    if (!scanning) awaitingFrame = false
    wasScanning = scanning
  }

  function clockText(now) {
    return Qt.formatTime(now, "hh") + "<font color='" + accentColor + "'>:</font>" + Qt.formatTime(now, "mm")
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
    if (!faceFailed) return
    passwordPanelRequested()
    if (animate) shakeAnim.restart()
  }
  onMatchedChanged: {
    if (matched && animate) pulseAnim.restart()
  }
  Component.onCompleted: syncPasswordText()

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

      Column {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(4)

        Text {
          id: clockLabel
          anchors.horizontalCenter: parent.horizontalCenter
          textFormat: Text.StyledText
          text: root.clockText(new Date())
          color: Color.lock.text
          font.family: Style.font.family
          font.pixelSize: Math.round(Style.font.heading * 3.4)
          font.weight: Font.Light
        }

        Text {
          id: dateLabel
          anchors.horizontalCenter: parent.horizontalCenter
          text: Qt.formatDate(new Date(), "dddd, MMMM d")
          color: Color.lock.placeholder
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
      }

      Item { width: 1; height: Style.space(4) }

      // Frosted card in the theme's lock colours; corners follow the theme radius.
      BorderSurface {
        id: card
        anchors.horizontalCenter: parent.horizontalCenter
        readonly property int pad: Style.space(26)
        width: Math.max(root.fieldWidth, cameraFrame.boxSize + Style.space(24)) + pad * 2
        height: cardContent.implicitHeight + pad * 2
        radius: Style.cornerRadius
        color: Color.lock.background
        borderSpec: Border.flat(Util.alpha(Color.lock.text, 0.1), 1)
        clip: true
        Behavior on height {
          enabled: root.animate
          NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
        }

        Column {
          id: cardContent
          x: card.pad
          y: card.pad
          width: card.width - card.pad * 2
          spacing: Style.space(14)

      // ---------------------------------------------------------------- camera stage
      Item {
        id: cameraFrame
        anchors.horizontalCenter: parent.horizontalCenter
        readonly property int boxSize: {
          var maxByHeight = Math.round(root.height * 0.34)
          var maxByWidth = Math.round(root.width * 0.42)
          var preferred = Style.space(220)
          return Math.max(Style.space(156), Math.min(preferred, maxByHeight, maxByWidth))
        }
        // Follow the theme's corner radius; sharp themes get a barely-rounded frame.
        readonly property int boxRadius: Style.cornerRadius > 0
          ? Math.min(Math.round(boxSize * 0.22), Style.cornerRadius * 3)
          : Math.round(boxSize * 0.05)
        readonly property int innerPad: 3
        readonly property int innerSize: Math.max(1, boxSize - innerPad * 2)
        readonly property int innerRadius: Math.max(1, boxRadius - innerPad)
        width: boxSize
        height: boxSize
        transform: Translate { id: shake }

        SequentialAnimation {
          id: shakeAnim
          NumberAnimation { target: shake; property: "x"; to: -9; duration: 55; easing.type: Easing.OutQuad }
          NumberAnimation { target: shake; property: "x"; to: 8; duration: 90; easing.type: Easing.InOutQuad }
          NumberAnimation { target: shake; property: "x"; to: -5; duration: 80; easing.type: Easing.InOutQuad }
          NumberAnimation { target: shake; property: "x"; to: 0; duration: 70; easing.type: Easing.OutQuad }
        }

        // Soft halo: always faintly present, blooms on a match.
        Rectangle {
          id: halo
          anchors.centerIn: parent
          width: parent.width + 18
          height: parent.height + 18
          radius: cameraFrame.boxRadius + 9
          color: "transparent"
          border.width: 5
          border.color: root.matched ? root.accentColor : (root.faceFailed ? root.softError : root.accentColor)
          opacity: root.matched ? 0.45 : (root.faceFailed ? 0.18 : 0.12)
          Behavior on opacity { NumberAnimation { duration: 260 } }
        }

        Rectangle {
          id: pulseRing
          anchors.centerIn: parent
          width: parent.width
          height: parent.height
          radius: cameraFrame.boxRadius
          color: "transparent"
          border.width: 3
          border.color: root.accentColor
          opacity: 0
          ParallelAnimation {
            id: pulseAnim
            NumberAnimation { target: pulseRing; property: "scale"; from: 1.0; to: 1.22; duration: 650; easing.type: Easing.OutCubic }
            NumberAnimation { target: pulseRing; property: "opacity"; from: 0.7; to: 0; duration: 650; easing.type: Easing.OutCubic }
          }
        }

        Rectangle {
          anchors.fill: parent
          radius: cameraFrame.boxRadius
          color: Util.alpha(Color.background, 0.92)
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

          // Not recognised: the last frame, frozen, blurred and dimmed. It is a
          // static image, so the blur is rendered once, not per frame.
          MultiEffect {
            anchors.fill: parent
            visible: (root.faceFailed || root.awaitingFrame) && root.frameReady
            source: root.shownBuffer === 0 ? previewA : previewB
            autoPaddingEnabled: false
            blurEnabled: true
            blur: 1.0
            blurMax: 48
            saturation: -0.7
            brightness: -0.35
          }

          Rectangle {
            anchors.fill: parent
            color: Color.background
            opacity: root.faceFailed ? (root.frameReady ? 0.25 : 0.6) : (root.awaitingFrame && root.frameReady ? 0.3 : 0)
            Behavior on opacity { NumberAnimation { duration: 220 } }
          }

          // Light sweep while scanning: a soft band instead of a hard line.
          Rectangle {
            id: sweep
            width: parent.width
            height: Math.round(parent.height * 0.28)
            visible: root.scanning && root.frameReady && !root.awaitingFrame
            gradient: Gradient {
              GradientStop { position: 0.0; color: "transparent" }
              GradientStop { position: 0.5; color: Util.alpha(root.accentColor, 0.2) }
              GradientStop { position: 1.0; color: "transparent" }
            }
            NumberAnimation on y {
              running: sweep.visible && root.animate
              loops: Animation.Infinite
              from: -sweep.height
              to: cameraContent.height
              duration: 2200
              easing.type: Easing.InOutSine
            }
          }

          // Welcome: the owner's photo fades in over the camera.
          Image {
            id: avatarImage
            anchors.fill: parent
            source: root.avatarPath ? "file://" + root.avatarPath : ""
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            sourceSize.width: cameraFrame.innerSize * 2
            sourceSize.height: cameraFrame.innerSize * 2
            opacity: root.matched && root.hasAvatar ? 1 : 0
            scale: root.matched ? 1.0 : 1.08
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: 280; easing.type: Easing.OutCubic } }
            Behavior on scale { NumberAnimation { duration: 420; easing.type: Easing.OutCubic } }
          }
        }

        // Viewfinder corners, breathing while scanning.
        Corners {
          anchors.fill: parent
          anchors.margins: Math.round(cameraFrame.boxSize * 0.09)
          length: Math.round(cameraFrame.boxSize * 0.13)
          thickness: 3
          color: root.accentColor
          visible: root.scanning
          SequentialAnimation on opacity {
            running: root.scanning && root.animate
            loops: Animation.Infinite
            NumberAnimation { from: 0.95; to: 0.4; duration: 1100; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.4; to: 0.95; duration: 1100; easing.type: Easing.InOutSine }
          }
        }

        // Starting camera: an accent arc spins around the camera glyph. The
        // RotationAnimator runs on the render thread, so it costs nothing on the UI thread.
        Item {
          id: spinner
          anchors.centerIn: parent
          width: Math.round(cameraFrame.boxSize * 0.34)
          height: width
          visible: root.scanning && (!root.frameReady || root.awaitingFrame)
          Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
              strokeColor: Util.alpha(root.accentColor, 0.18)
              strokeWidth: 3
              fillColor: "transparent"
              PathAngleArc { centerX: spinner.width / 2; centerY: spinner.height / 2; radiusX: spinner.width / 2 - 2; radiusY: radiusX; startAngle: 0; sweepAngle: 360 }
            }
            ShapePath {
              strokeColor: root.accentColor
              strokeWidth: 3
              capStyle: ShapePath.RoundCap
              fillColor: "transparent"
              PathAngleArc { centerX: spinner.width / 2; centerY: spinner.height / 2; radiusX: spinner.width / 2 - 2; radiusY: radiusX; startAngle: -90; sweepAngle: 100 }
            }
            RotationAnimator on rotation {
              running: spinner.visible && root.animate
              from: 0; to: 360; duration: 900; loops: Animation.Infinite
            }
          }
        }

        Text {
          anchors.centerIn: parent
          visible: spinner.visible
          text: "󰄀"
          color: root.accentColor
          font.family: Style.font.family
          font.pixelSize: Math.round(cameraFrame.boxSize * 0.18)
          SequentialAnimation on opacity {
            running: spinner.visible && root.animate
            loops: Animation.Infinite
            NumberAnimation { from: 0.9; to: 0.35; duration: 900; easing.type: Easing.InOutSine }
            NumberAnimation { from: 0.35; to: 0.9; duration: 900; easing.type: Easing.InOutSine }
          }
        }

        // Not recognised: a small neutral face in a viewfinder, and a retry hint.
        Column {
          anchors.centerIn: parent
          spacing: Style.space(12)
          visible: root.faceFailed
          opacity: visible ? 1 : 0
          Behavior on opacity { NumberAnimation { duration: 220 } }

          Item {
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.round(cameraFrame.boxSize * 0.3)
            height: width

            Corners {
              anchors.fill: parent
              length: Math.round(parent.width * 0.3)
              thickness: 3
              color: Color.lock.text
              opacity: 0.85
            }
            Row {
              anchors.horizontalCenter: parent.horizontalCenter
              y: Math.round(parent.height * 0.36)
              spacing: Math.round(parent.width * 0.22)
              Repeater {
                model: 2
                Rectangle { width: 5; height: 5; radius: 2.5; color: Color.lock.text; opacity: 0.85 }
              }
            }
            Rectangle {
              anchors.horizontalCenter: parent.horizontalCenter
              y: Math.round(parent.height * 0.64)
              width: Math.round(parent.width * 0.32)
              height: 3
              radius: 1.5
              color: Color.lock.text
              opacity: 0.85
            }
          }

          Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            visible: root.faceState === "failed"
            width: retryLabel.implicitWidth + Style.space(20)
            height: retryLabel.implicitHeight + Style.space(10)
            radius: height / 2
            color: Util.alpha(Color.background, 0.55)
            border.width: 1
            border.color: Util.alpha(Color.lock.text, 0.18)
            Text {
              id: retryLabel
              anchors.centerIn: parent
              text: "Tap to try again"
              color: Color.lock.text
              font.family: Style.font.family
              font.pixelSize: Math.round(Style.font.body * 0.85)
            }
          }
        }

        // Frame outline: the theme's Hyprland active-window border while scanning.
        BorderSurface {
          anchors.fill: parent
          radius: cameraFrame.boxRadius
          color: "transparent"
          borderSpec: root.matched ? Border.flat(root.accentColor, 3)
            : root.faceFailed ? Border.flat(root.softError, 2)
            : Border.hyprlandActiveSpec(root.accentColor, 2)
        }

        // Check badge on a match.
        Rectangle {
          width: Math.round(cameraFrame.boxSize * 0.17)
          height: width
          radius: width / 2
          x: cameraFrame.boxSize - width * 0.8
          y: cameraFrame.boxSize - width * 0.8
          color: root.accentColor
          border.width: 3
          border.color: Color.background
          scale: root.matched ? 1 : 0
          visible: scale > 0
          Behavior on scale { NumberAnimation { duration: 320; easing.type: Easing.OutBack } }
          Text {
            anchors.centerIn: parent
            text: "✓"
            color: Color.background
            font.pixelSize: Math.round(parent.width * 0.55)
            font.bold: true
          }
        }

        MouseArea {
          anchors.fill: parent
          enabled: root.faceState === "failed" && root.inputEnabled
          cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: root.retryRequested()
        }
      }

      Item { width: 1; height: Style.space(4) }

      // ---------------------------------------------------------------- status
      Column {
        anchors.horizontalCenter: parent.horizontalCenter
        width: parent.width
        spacing: Style.space(4)
        // Keeps text legible over bright wallpapers; re-rendered only when text changes.
        layer.enabled: root.animate
        layer.effect: MultiEffect {
          shadowEnabled: true
          shadowColor: Color.background
          shadowOpacity: 0.95
          shadowBlur: 0.7
          shadowVerticalOffset: 1
          shadowHorizontalOffset: 0
        }

        Text {
          id: titleLabel
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
          text: root.statusLine + (root.scanning && root.frameReady && !root.awaitingFrame ? dots.text : "")
          color: root.matched ? root.accentColor : Color.lock.text
          font.family: Style.font.family
          font.pixelSize: root.matched ? Math.round(Style.font.heading * 1.35) : Math.round(Style.font.heading * 1.05)
          font.weight: root.matched ? Font.DemiBold : Font.Medium
          Behavior on font.pixelSize { NumberAnimation { duration: 200 } }
        }

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
          text: root.statusDetail
          color: Util.alpha(Color.lock.text, 0.82)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }

        QtObject {
          id: dots
          property int count: 0
          readonly property string text: count === 0 ? "" : " " + "·".repeat(count)
        }
        Timer {
          interval: 420
          repeat: true
          running: root.scanning && root.frameReady && root.animate
          onTriggered: dots.count = (dots.count + 1) % 4
        }
      }

      Item {
        width: parent.width
        height: root.showPasswordField ? inputField.height + Style.space(6) : 0
        visible: root.showPasswordField

        BorderSurface {
          id: inputField
          y: Style.space(6)
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
        }
      }
    }

    Button {
      id: passwordButton
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Math.round(Style.space(28))
      text: "Type password"
      iconText: "󰌾"
      bordered: true
      enabled: root.inputEnabled
      visible: !root.matched
      onClicked: {
        root.wakeRequested()
        root.openPassword()
      }
    }
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.loadBackground
    onTriggered: {
      var now = new Date()
      clockLabel.text = root.clockText(now)
      dateLabel.text = Qt.formatDate(now, "dddd, MMMM d")
    }
  }
}
