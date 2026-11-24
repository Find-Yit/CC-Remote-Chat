#!/bin/bash
# 生成 ~/Applications/CC Remote Chat.app（docs/SPEC_V2_TUI.md §14）。
# - 有 xcrun swiftc：编译原生应用 tools/macapp/main.swift（AppKit + WKWebView）。程序坞、Cmd+Tab、最小化、
#   窗口菜单、关于面板都显示「CC Remote Chat」；后端 python3 app.py 仍是独立的后台进程（关窗口/退出应用都不停）。
# - 没有 swiftc 或编译失败：回退为原来的 shell 启动壳（后台运行 python3 app.py → Chrome 应用窗口）。
# 本脚本不安装任何其他东西、不碰 ~/.claude。
#
# 用法：tools/make_app.sh [目标目录，默认 ~/Applications]
# 环境变量：
#   PYTHON=/path/to/python3   默认取当前 PATH 中的 python3 的绝对路径
#   CCR_NO_NATIVE=1           不编译原生应用，直接生成 shell 启动壳
# 仅自测用（正式版不要设置；只对原生应用生效）：
#   CCR_BUNDLE_ID=local.cc-remote-chat.test   bundle id（默认 local.cc-remote-chat）
#   CCR_DATA_DIR=/tmp/…/data                  数据目录（Info.plist CCRDataDir；后端加 --data-dir）
#   CCR_ARGS="--fake --port 18765 --no-sound" 追加给 app.py 的参数（空格分隔）
#   CCR_SANDBOX_PROFILE=/tmp/….sb             后端用 /usr/bin/sandbox-exec -f <profile> 运行
#   CCR_DOWNLOAD_DIR=/tmp/…/downloads         下载目录（默认 ~/Downloads）
#   CCR_SELFTEST=1                            编译自测钩子（-D CCR_SELFTEST：命令文件驱动，外部链接只记录不打开）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP_NAME="CC Remote Chat"
APP="$DEST_DIR/$APP_NAME.app"
# 可执行文件名 = CFBundleExecutable。原生应用用「CC Remote Chat」（ps、活动监视器、System Events 里的进程名也一致）；
# shell 启动壳保持原来的 cc-remote-chat
EXE_NAME="cc-remote-chat"
NATIVE_EXE_NAME="$APP_NAME"
SWIFT_SRC="$PROJECT_ROOT/tools/macapp/main.swift"

PY="${PYTHON:-$(command -v python3 || true)}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "找不到 python3，请用 PYTHON=/path/to/python3 指定" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "需要 Python 3.9+（当前：$("$PY" --version 2>&1)）" >&2
  exit 1
fi
if [ ! -f "$PROJECT_ROOT/app.py" ]; then
  echo "找不到 $PROJECT_ROOT/app.py" >&2
  exit 1
fi
VERSION="$(git -C "$PROJECT_ROOT" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || true)"
VERSION="${VERSION:-1.0}"

# 生成 shell 单引号字面量
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

new_bundle() {
  mkdir -p "$DEST_DIR"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  # 图标（assets/AppIcon.icns，由 assets/icon.svg 生成）
  if [ -f "$PROJECT_ROOT/assets/AppIcon.icns" ]; then
    cp "$PROJECT_ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
  fi
}

register_bundle() {
  touch "$APP"
  [ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 || true
}

# Info.plist 用 plistlib 生成（路径里的 & < > 引号都不用手工转义）
# 参数：模式(native|shell) bundle_id 数据目录 沙箱profile 下载目录 [给 app.py 的额外参数…]
write_plist() {
  "$PY" -I - "$APP/Contents/Info.plist" "$1" "$2" "$VERSION" "$PY" "$PROJECT_ROOT/app.py" "$EXE_NAME" \
    "$3" "$4" "$5" "${@:6}" <<'PYEOF'
import plistlib, sys
out, mode, bundle_id, version, py, app_py, exe, data_dir, sandbox, dl_dir = sys.argv[1:11]
extra = sys.argv[11:]
d = {
    "CFBundleName": "CC Remote Chat",
    "CFBundleDisplayName": "CC Remote Chat",
    "CFBundleIdentifier": bundle_id,
    "CFBundleVersion": version,
    "CFBundleShortVersionString": version,
    "CFBundlePackageType": "APPL",
    "CFBundleExecutable": exe,
    "CFBundleIconFile": "AppIcon",
}
if mode == "native":
    d.update({
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleAllowMixedLocalizations": True,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSSupportsAutomaticTermination": False,
        "CCRPython": py,
        "CCRAppPy": app_py,
    })
    if data_dir:
        d["CCRDataDir"] = data_dir
    if extra:
        d["CCRArgs"] = extra
    if sandbox:
        d["CCRSandboxProfile"] = sandbox
    if dl_dir:
        d["CCRDownloadDir"] = dl_dir
else:
    d.update({"LSUIElement": True, "LSMinimumSystemVersion": "11.0"})
with open(out, "wb") as f:
    plistlib.dump(d, f)
PYEOF
}

make_shell_app() {
  new_bundle
  write_plist shell "local.cc-remote-chat" "" "" ""
  cat > "$APP/Contents/MacOS/$EXE_NAME" <<LAUNCH
#!/bin/bash
# 由 tools/make_app.sh 生成（shell 启动壳，没有 swiftc 时的回退方案）。已有实例时 app.py 只会让它重新打开窗口。
# 注意：本 bundle 的主进程必须马上退出。若 exec 成长驻的 python（没有 Cocoa 事件循环、不回应 Apple Event），
# 第二次双击时 macOS 会报「你不能打开应用程序…因为它没有响应」。所以把 app.py 放到后台运行后立即退出。
PY=$(q "$PY")
APP_PY=$(q "$PROJECT_ROOT/app.py")
LOG_DIR="\$HOME/.cc-remote-chat/logs"
mkdir -p "\$LOG_DIR" && chmod 700 "\$HOME/.cc-remote-chat" "\$LOG_DIR" 2>/dev/null
# Finder 启动的进程 PATH 很短，补上常见路径以便找到 ssh/open
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:\$PATH"
nohup "\$PY" "\$APP_PY" >>"\$LOG_DIR/launcher.log" 2>&1 </dev/null &
disown 2>/dev/null
exit 0
LAUNCH
  chmod 755 "$APP/Contents/MacOS/$EXE_NAME"
  register_bundle
  echo "已生成（shell 启动壳 → Chrome 应用窗口）：$APP"
}

# 编译原生应用；成功返回 0（可执行文件在 $BUILD_DIR/$NATIVE_EXE_NAME）
build_native() {
  [ -f "$SWIFT_SRC" ] || { echo "找不到 $SWIFT_SRC" >&2; return 1; }
  command -v xcrun >/dev/null 2>&1 || { echo "没有 xcrun（未安装 Xcode 命令行工具）" >&2; return 1; }
  xcrun --find swiftc >/dev/null 2>&1 || { echo "xcrun 找不到 swiftc" >&2; return 1; }
  local flags=(-O -swift-version 5 -target "$(uname -m)-apple-macos12.0" -framework Cocoa -framework WebKit)
  [ "${CCR_SELFTEST:-}" = "1" ] && flags+=(-D CCR_SELFTEST)
  echo "正在编译原生应用（swiftc）…"
  xcrun swiftc "${flags[@]}" -o "$BUILD_DIR/$NATIVE_EXE_NAME" "$SWIFT_SRC"
}

make_native_app() {
  local bundle_id="${CCR_BUNDLE_ID:-local.cc-remote-chat}"
  local extra=()
  if [ -n "${CCR_ARGS:-}" ]; then read -r -a extra <<< "$CCR_ARGS"; fi
  new_bundle
  EXE_NAME="$NATIVE_EXE_NAME"
  install -m 755 "$BUILD_DIR/$EXE_NAME" "$APP/Contents/MacOS/$EXE_NAME"
  write_plist native "$bundle_id" "${CCR_DATA_DIR:-}" "${CCR_SANDBOX_PROFILE:-}" "${CCR_DOWNLOAD_DIR:-}" \
    ${extra[@]+"${extra[@]}"}
  # ad-hoc 签名（Apple Silicon 上未签名的原生可执行文件不能运行；也把 Info.plist 绑定进签名）
  if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || echo "警告：ad-hoc 签名失败" >&2
  else
    echo "警告：没有 codesign，未签名" >&2
  fi
  register_bundle
  echo "已生成（原生应用，bundle id ${bundle_id}）：$APP"
  if [ -n "${CCR_DATA_DIR:-}${CCR_ARGS:-}${CCR_SANDBOX_PROFILE:-}${CCR_DOWNLOAD_DIR:-}${CCR_SELFTEST:-}" ]; then
    echo "（自测版：数据目录=${CCR_DATA_DIR:-默认} 参数=${CCR_ARGS:-无} 沙箱=${CCR_SANDBOX_PROFILE:-无} 自测钩子=${CCR_SELFTEST:-0}）"
  fi
}

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ccr-make-app.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT
if [ "${CCR_NO_NATIVE:-}" = "1" ]; then
  make_shell_app
elif build_native; then
  make_native_app
else
  echo "原生应用编译失败或没有 swiftc，回退为 shell 启动壳（窗口仍是 Chrome 应用窗口）" >&2
  make_shell_app
fi

echo "Python：$PY"
echo "项目：$PROJECT_ROOT"
if [ -n "${CCR_DATA_DIR:-}" ] && [ "${CCR_NO_NATIVE:-}" != "1" ]; then
  echo "退出运行中的后端：\"$PY\" \"$PROJECT_ROOT/app.py\" --data-dir \"$CCR_DATA_DIR\" --stop"
else
  echo "退出运行中的后端：\"$PY\" \"$PROJECT_ROOT/app.py\" --stop"
fi
