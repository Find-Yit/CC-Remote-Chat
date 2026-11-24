#!/bin/bash
# 生成可以直接发给别人的 dmg（内置模式，docs/SPEC_V2_TUI.md §14.10）：Apple 芯片（arm64）、macOS 12+，
# 对方只需自己装好 Claude Code 并登录过一次；不需要 Homebrew、Python、tmux 或任何配置。
#
#   dmg 里：CC Remote Chat.app、指向 /Applications 的链接、先读我.txt
#   .app 里：Contents/MacOS/CC Remote Chat（原生壳，Info.plist CCRBundled=true）
#           Contents/Resources/{AppIcon.icns, python/（裁剪过的 python-build-standalone）, app/（只放运行需要的代码 + BUILD）,
#                               bin/tmux（静态链接 libevent + utf8proc，系统 ncurses）, licenses/}
#
# 用法：tools/make_dmg.sh [--version X.Y] [--out 目录] [--release-key 目录 [--update-repo owner/name]]
#   版本号：--version > git describe --tags > 项目根目录的 VERSION 文件（开源版没有 git 时用）> 0.0
#   输出：<out，默认 <项目>/dist>/CC Remote Chat <版本>.dmg
#   --release-key：用 tools/release_key.sh init 生成的自签名证书签名（不给 = ad-hoc 签名）。
#   --update-repo：开启自动更新（SPEC §14.11），把 {repo, cert_sha1, cert_sha256} 写进 Resources/app/UPDATE.json
#                  （签名之前写，受签名保护）；必须同时给 --release-key。不给 = 自动更新关闭（别人自己从开源版打包时）。
# 下载件（固定版本、固定 URL、校验 SHA256）缓存在 <项目>/build/dmg-cache/（.gitignore 已忽略；可用 CCR_DMG_CACHE 改）。
#
# 本脚本：不弹任何 GUI、不用 open、不用 Finder/AppleScript 布置 dmg 窗口；不碰 ~/.claude*、不碰数据目录；
# 整个组装过程在 mktemp -d /tmp/ccr-dmg.XXXXXX 里进行（pyc、二进制里不会出现 /Users/<名字>）。
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin   # 不让 Homebrew 的工具 / pkg-config / python 混进构建
export LC_ALL=C

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="CC Remote Chat"
BUNDLE_ID="local.cc-remote-chat.bundled"   # 不能和 tools/make_app.sh 生成的 local.cc-remote-chat 冲突
DATA_DIR_DEFAULT="~/.cc-remote-chat-app"    # 内置模式的数据目录（不带空格；与自己装的 ~/.cc-remote-chat 分开）
MIN_MACOS="12.0"

VERSION=""
OUT_DIR="$PROJECT_ROOT/dist"
RELEASE_KEY=""
UPDATE_REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --release-key) RELEASE_KEY="${2:?}"; shift 2 ;;
    --update-repo) UPDATE_REPO="${2:?}"; shift 2 ;;
    --version) VERSION="${2:?}"; shift 2 ;;
    --version=*) VERSION="${1#--version=}"; shift ;;
    --out) OUT_DIR="${2:?}"; shift 2 ;;
    --out=*) OUT_DIR="${1#--out=}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
if [ -z "$VERSION" ]; then
  VERSION="$(git -C "$PROJECT_ROOT" describe --tags 2>/dev/null || true)"
fi
if [ -z "$VERSION" ] && [ -f "$PROJECT_ROOT/VERSION" ]; then
  VERSION="$(head -n1 "$PROJECT_ROOT/VERSION" | tr -d '[:space:]')"
fi
VERSION="${VERSION#v}"
VERSION="${VERSION:-0.0}"
case "$VERSION" in
  *[!A-Za-z0-9._-]*) echo "版本号只能包含字母、数字和 . _ -：$VERSION" >&2; exit 2 ;;
esac
SHORT_VERSION="$(printf '%s' "$VERSION" | sed -E 's/^([0-9]+(\.[0-9]+)*).*/\1/')"
# 签名身份：发布密钥（自签名证书，钥匙串不在搜索列表里，用 --keychain 指定）或 ad-hoc
SIGN_ID="-"; SIGN_KC=""; CERT_SHA1=""; CERT_SHA256=""
if [ -n "$UPDATE_REPO" ] && [ -z "$RELEASE_KEY" ]; then
  echo "--update-repo 需要同时给 --release-key（自动更新靠钉死的证书验证新版本）" >&2; exit 2
fi
if [ -n "$UPDATE_REPO" ] && ! printf '%s' "$UPDATE_REPO" | grep -qE '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$'; then
  echo "--update-repo 格式不对（owner/name）：$UPDATE_REPO" >&2; exit 2
fi
if [ -n "$RELEASE_KEY" ]; then
  SIGN_KC="$RELEASE_KEY/release.keychain-db"
  [ -f "$SIGN_KC" ] && [ -f "$RELEASE_KEY/cert.pem" ] && [ -f "$RELEASE_KEY/keychain-password" ] || {
    echo "发布密钥目录不完整：$RELEASE_KEY（先运行 tools/release_key.sh init）" >&2; exit 1; }
  CERT_SHA1="$(/usr/bin/openssl x509 -in "$RELEASE_KEY/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')"
  CERT_SHA256="$(/usr/bin/openssl x509 -in "$RELEASE_KEY/cert.pem" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')"
  SIGN_ID="$CERT_SHA1"
fi
case "$SHORT_VERSION" in [0-9]*) ;; *) SHORT_VERSION="0.0" ;; esac

# ---------------------------------------------------------------- 下载件（固定版本 + SHA256）
PY_FILE="cpython-3.12.16+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA="2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd"
TMUX_FILE="tmux-3.6a.tar.gz"
TMUX_URL="https://github.com/tmux/tmux/releases/download/3.6a/tmux-3.6a.tar.gz"
TMUX_SHA="b6d8d9c76585db8ef5fa00d4931902fa4b8cbe8166f528f44fc403961a3f3759"
LIBEVENT_FILE="libevent-2.1.12-stable.tar.gz"
LIBEVENT_URL="https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz"
LIBEVENT_SHA="92e6de1be9ec176428fd2367677e61ceffc2ee1cb119035037a27d346b0403bb"
UTF8PROC_FILE="utf8proc-2.11.3.tar.gz"
UTF8PROC_URL="https://github.com/JuliaStrings/utf8proc/releases/download/v2.11.3/utf8proc-2.11.3.tar.gz"
UTF8PROC_SHA="415189fd2c85cd6ee5ff26af500fa387de9ada1e3e316e93f7338551481d557d"

CACHE="${CCR_DMG_CACHE:-$PROJECT_ROOT/build/dmg-cache}"

# 构建时要运行打包进去的 arm64 Python（预编译 pyc、写 Info.plist），tmux 的 configure 也要能运行 arm64 测试程序
[ "$(uname -m)" = "arm64" ] || { echo "需要在 Apple 芯片（arm64）的 Mac 上运行" >&2; exit 1; }
for t in curl shasum tar xcrun codesign hdiutil ditto rsync file otool; do
  command -v "$t" >/dev/null 2>&1 || { echo "缺少工具：$t（需要 Xcode 命令行工具）" >&2; exit 1; }
done
xcrun --find clang >/dev/null 2>&1 || { echo "xcrun 找不到 clang（请安装 Xcode 命令行工具）" >&2; exit 1; }
xcrun --find swiftc >/dev/null 2>&1 || { echo "xcrun 找不到 swiftc（请安装 Xcode 命令行工具）" >&2; exit 1; }
[ -f "$PROJECT_ROOT/app.py" ] && [ -f "$PROJECT_ROOT/tools/macapp/main.swift" ] || { echo "项目目录不完整：$PROJECT_ROOT" >&2; exit 1; }

fetch() {   # fetch <文件名> <URL> <SHA256>：缓存里有且校验通过就直接用，否则下载并校验
  local f="$CACHE/$1"
  mkdir -p "$CACHE"
  if [ -f "$f" ] && [ "$(shasum -a 256 "$f" | cut -d' ' -f1)" = "$3" ]; then
    return 0
  fi
  echo "下载 $1 …"
  rm -f "$f.part"
  curl -fsSL --retry 3 --connect-timeout 30 -o "$f.part" "$2"
  local got
  got="$(shasum -a 256 "$f.part" | cut -d' ' -f1)"
  if [ "$got" != "$3" ]; then
    rm -f "$f.part"
    echo "SHA256 不匹配：$1（期望 $3，实际 $got）" >&2
    exit 1
  fi
  mv -f "$f.part" "$f"
}
fetch "$PY_FILE" "$PY_URL" "$PY_SHA"
fetch "$TMUX_FILE" "$TMUX_URL" "$TMUX_SHA"
fetch "$LIBEVENT_FILE" "$LIBEVENT_URL" "$LIBEVENT_SHA"
fetch "$UTF8PROC_FILE" "$UTF8PROC_URL" "$UTF8PROC_SHA"

# ---------------------------------------------------------------- 暂存目录（全部在 /tmp 下，避免路径里出现用户名）
STAGE="$(mktemp -d /tmp/ccr-dmg.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/root/$APP_NAME.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/bin" "$RES/licenses"
echo "暂存目录：$STAGE"

# ---------------------------------------------------------------- tmux（arm64，macOS 12+）
logged() {   # logged <日志> <命令…>：输出进日志；失败时打印日志末尾并退出
  local log="$1"; shift
  if ! "$@" >>"$log" 2>&1; then
    echo "失败：$*" >&2; tail -n 30 "$log" >&2; exit 1
  fi
}

build_tmux() {
  local B="$STAGE/tmux-build" SRC DEPS
  SRC="$B/src"; DEPS="$B/deps"
  mkdir -p "$SRC" "$DEPS/lib" "$DEPS/include"
  for f in "$TMUX_FILE" "$LIBEVENT_FILE" "$UTF8PROC_FILE"; do tar -xzf "$CACHE/$f" -C "$SRC"; done
  (   # 子 shell：编译用的环境变量不外泄；任何一步失败 logged 都会 exit 1，主脚本随之退出（set -e）
    export MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS"
    CC="$(xcrun -f clang)"; export CC
    SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"; export SDKROOT
    export CFLAGS="-arch arm64 -mmacosx-version-min=$MIN_MACOS -O2"
    export LDFLAGS="-arch arm64 -mmacosx-version-min=$MIN_MACOS"
    export PKG_CONFIG_LIBDIR="$DEPS/lib/pkgconfig" PKG_CONFIG_PATH="$DEPS/lib/pkgconfig"   # 看不到 Homebrew 的 .pc
    echo "编译 libevent（静态，仅 core）…"
    cd "$SRC/libevent-2.1.12-stable"
    logged "$B/libevent.log" ./configure --prefix="$DEPS" --disable-shared --enable-static --disable-openssl \
      --disable-samples --disable-libevent-regress --disable-debug-mode --disable-dependency-tracking
    logged "$B/libevent.log" make -j8
    logged "$B/libevent.log" make install
    echo "编译 utf8proc（静态）…"
    cd "$SRC/utf8proc-2.11.3"
    logged "$B/utf8proc.log" make -j8 libutf8proc.a CC="$CC" CFLAGS="$CFLAGS"
    cp libutf8proc.a "$DEPS/lib/"
    cp utf8proc.h "$DEPS/include/"
    echo "编译 tmux 3.6a …"
    cd "$SRC/tmux-3.6a"
    logged "$B/tmux.log" ./configure --prefix=/ccr-tmux --sysconfdir=/etc --enable-utf8proc \
      --disable-dependency-tracking \
      CPPFLAGS="-I$DEPS/include" LDFLAGS="$LDFLAGS -L$DEPS/lib" \
      LIBEVENT_CORE_CFLAGS="-I$DEPS/include" LIBEVENT_CORE_LIBS="$DEPS/lib/libevent_core.a" \
      LIBUTF8PROC_CFLAGS="-I$DEPS/include" LIBUTF8PROC_LIBS="$DEPS/lib/libutf8proc.a"
    logged "$B/tmux.log" make -j8
  )
  install -m 755 "$SRC/tmux-3.6a/tmux" "$RES/bin/tmux"
  strip -x "$RES/bin/tmux"
  cp "$SRC/tmux-3.6a/COPYING" "$RES/licenses/tmux-COPYING.txt"
  cp "$SRC/libevent-2.1.12-stable/LICENSE" "$RES/licenses/libevent-LICENSE.txt"
  cp "$SRC/utf8proc-2.11.3/LICENSE.md" "$RES/licenses/utf8proc-LICENSE.md"
}
build_tmux
if otool -L "$RES/bin/tmux" | tail -n +2 | grep -vqE '^\s+(/usr/lib/|/System/)'; then
  echo "tmux 依赖了系统以外的动态库：" >&2; otool -L "$RES/bin/tmux" >&2; exit 1
fi

# ---------------------------------------------------------------- 原生壳（源码先拷进 /tmp 再编译）
echo "编译原生壳（swiftc，arm64-apple-macos$MIN_MACOS）…"
cp "$PROJECT_ROOT/tools/macapp/main.swift" "$STAGE/main.swift"
(cd "$STAGE" && xcrun swiftc -O -swift-version 5 -target "arm64-apple-macos$MIN_MACOS" \
  -framework Cocoa -framework WebKit -o "$STAGE/shell" main.swift)
install -m 755 "$STAGE/shell" "$APP/Contents/MacOS/$APP_NAME"
cp "$PROJECT_ROOT/assets/AppIcon.icns" "$RES/AppIcon.icns"

# ---------------------------------------------------------------- Python（裁剪）
echo "解压并裁剪 Python …"
tar -xzf "$CACHE/$PY_FILE" -C "$STAGE"
PYSRC="$STAGE/python"
PL="$PYSRC/lib/python3.12"
cp "$PL/LICENSE.txt" "$RES/licenses/python-LICENSE.txt"
# lib 下只留 python3.12/（去掉 libpython3.12.dylib —— bin/python3.12 是静态链接的 —— 以及 tcl/tk/itcl/thread、pkgconfig）
find "$PYSRC/lib" -mindepth 1 -maxdepth 1 ! -name python3.12 -exec rm -rf {} +
rm -rf "$PYSRC/include" "$PYSRC/share" \
  "$PL/idlelib" "$PL/tkinter" "$PL/turtledemo" "$PL/turtle.py" "$PL/ensurepip" "$PL/lib2to3" "$PL/pydoc_data" \
  "$PL/venv" "$PL/test" "$PL/idle_test" "$PL"/config-3.12-darwin "$PL"/site-packages/pip* \
  "$PL"/lib-dynload/_tkinter*.so
# bin 只留 python3.12 与 python3 -> python3.12
find "$PYSRC/bin" -mindepth 1 ! -name python3.12 ! -name python3 -exec rm -f {} +
[ -L "$PYSRC/bin/python3" ] || ln -sf python3.12 "$PYSRC/bin/python3"
find "$PYSRC" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$PYSRC" -name '*.pyc' -delete
mv "$PYSRC" "$RES/python"

# ---------------------------------------------------------------- app 代码（只放运行需要的）
echo "拷贝 app 代码 …"
mkdir -p "$RES/app"
cp "$PROJECT_ROOT/app.py" "$RES/app/app.py"
RSYNC_X=(--exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store)
rsync -a "${RSYNC_X[@]}" "$PROJECT_ROOT/ccr/" "$RES/app/ccr/"
rsync -a "${RSYNC_X[@]}" --exclude /dev/ "$PROJECT_ROOT/static/" "$RES/app/static/"
rsync -a "${RSYNC_X[@]}" "$PROJECT_ROOT/assets/" "$RES/app/assets/"
for f in LICENSE THIRD_PARTY_NOTICES.md; do   # 开源版有这两个文件；没有就跳过
  if [ -f "$PROJECT_ROOT/$f" ]; then cp "$PROJECT_ROOT/$f" "$RES/licenses/$f"; fi
done
cat > "$RES/licenses/BUNDLED_COMPONENTS.txt" <<EOF
CC Remote Chat $VERSION（dmg 内置版）随附的第三方组件
=====================================================

本 .app 自带下面这些组件，许可证全文见本目录同名文件：

- CPython 3.12.16（python-build-standalone 20261110，aarch64-apple-darwin install_only_stripped，已裁剪）
  许可证：Python Software Foundation License（python-LICENSE.txt，含 CPython 自带的第三方说明）
  来源：$PY_URL
  SHA256：$PY_SHA
  该发行版静态链接了 OpenSSL（Apache-2.0）、SQLite（公有领域）、libffi（MIT）、xz/liblzma（0BSD/公有领域）、
  bzip2（BSD 类）、mpdecimal（BSD-2-Clause）、expat（MIT）、zstd（BSD-3-Clause）等库，许可证见
  https://github.com/astral-sh/python-build-standalone 。

- tmux 3.6a — ISC License（tmux-COPYING.txt）
  来源：$TMUX_URL
  SHA256：$TMUX_SHA

- libevent 2.1.12-stable（静态链接进 tmux）— BSD-3-Clause（libevent-LICENSE.txt）
  来源：$LIBEVENT_URL
  SHA256：$LIBEVENT_SHA

- utf8proc 2.11.3（静态链接进 tmux）— MIT License + Unicode 数据许可（utf8proc-LICENSE.md）
  来源：$UTF8PROC_URL
  SHA256：$UTF8PROC_SHA

tmux 还动态链接 macOS 自带的 ncurses（/usr/lib/libncurses.5.4.dylib）。
EOF

# 构建标识（app.py --bundled 写进 app.pid）：版本 + 构建时间 + 代码哈希，同版本重新打包也不同。新 .app 原地替换旧 .app
# 后再打开，app.py 发现正在跑的后台标识不同（2.9 及更早的没有标识）→ 停掉旧后台并接管（SPEC §14.10「升级」）
APP_HASH="$(cd "$RES/app" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -c1-16)"
printf '%s %s %s\n' "$VERSION" "$(date -u +%Y%m%dT%H%M%SZ)" "$APP_HASH" > "$RES/app/BUILD"
# 自动更新的来源与钉死的证书指纹（SPEC §14.11）；同样在签名之前写
if [ -n "$UPDATE_REPO" ]; then
  printf '{"cert_sha1": "%s", "cert_sha256": "%s", "repo": "%s"}\n' "$CERT_SHA1" "$CERT_SHA256" "$UPDATE_REPO" \
    > "$RES/app/UPDATE.json"
  echo "自动更新：$UPDATE_REPO（证书 SHA-1 $CERT_SHA1）"
fi

# ---------------------------------------------------------------- 预编译 pyc（unchecked-hash：运行时不看 mtime、也不会回写）
echo "预编译 pyc …"
env -i PATH=/usr/bin:/bin HOME="$STAGE" "$RES/python/bin/python3" -I -m compileall -q -j 0 \
  --invalidation-mode unchecked-hash -s "$RES" -p / "$RES/python/lib/python3.12" "$RES/app" >/dev/null

# ---------------------------------------------------------------- Info.plist
env -i PATH=/usr/bin:/bin "$RES/python/bin/python3" -I -B - "$APP/Contents/Info.plist" "$BUNDLE_ID" "$VERSION" \
  "$SHORT_VERSION" "$DATA_DIR_DEFAULT" "$MIN_MACOS" "$APP_NAME" <<'PYEOF'
import plistlib, sys
out, bundle_id, version, short, data_dir, min_macos, name = sys.argv[1:8]
why = "Claude Code 在这里读写你的项目文件时需要（只在你让 claude 打开该文件夹里的项目时才会访问）。"
d = {
    "CFBundleName": name,
    "CFBundleDisplayName": name,
    "CFBundleIdentifier": bundle_id,
    "CFBundleVersion": short,
    "CFBundleShortVersionString": short,
    "CFBundleGetInfoString": "%s %s" % (name, version),
    "CFBundlePackageType": "APPL",
    "CFBundleExecutable": name,
    "CFBundleIconFile": "AppIcon",
    "CFBundleDevelopmentRegion": "zh_CN",
    "CFBundleAllowMixedLocalizations": True,
    "LSMinimumSystemVersion": min_macos,
    "LSArchitecturePriority": ["arm64"],
    "LSApplicationCategoryType": "public.app-category.developer-tools",
    "NSHighResolutionCapable": True,
    "NSPrincipalClass": "NSApplication",
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "NSSupportsAutomaticTermination": False,
    "NSDocumentsFolderUsageDescription": why,
    "NSDesktopFolderUsageDescription": why,
    "NSDownloadsFolderUsageDescription": why,
    "NSRemovableVolumesUsageDescription": why,
    "NSNetworkVolumesUsageDescription": why,
    "CCRBundled": True,
    "CCRDataDir": data_dir,
    "CCRVersion": version,
}
with open(out, "wb") as f:
    plistlib.dump(d, f)
PYEOF

# ---------------------------------------------------------------- 权限、扩展属性
chmod -R u+rwX,go+rX,go-w "$APP"
xattr -cr "$APP"

# ---------------------------------------------------------------- 签名：从里到外逐个签所有 Mach-O，最后签外层 app
# （里面没有嵌套的 bundle/framework，只有独立的 Mach-O 文件；find -depth 先列深层文件。不用 --deep 签名。）
# 发布密钥：钥匙串用密码解锁（-p，不弹 GUI），签完立即上锁；不加入搜索列表，codesign 用 --keychain 指定。
MAIN_EXE="$APP/Contents/MacOS/$APP_NAME"
SIGN_ARGS=(-f -s "$SIGN_ID" --timestamp=none)
if [ -n "$SIGN_KC" ]; then
  echo "用发布密钥签名（证书 SHA-1 $CERT_SHA1）…"
  security unlock-keychain -p "$(head -n1 "$RELEASE_KEY/keychain-password")" "$SIGN_KC"
  trap 'security lock-keychain "$SIGN_KC" 2>/dev/null || true; rm -rf "$STAGE"' EXIT
  SIGN_ARGS+=(--keychain "$SIGN_KC")
else
  echo "ad-hoc 签名 …"
fi
sign() {   # 签名；"replacing existing signature" 之类的正常输出不打印，失败才打印
  local out
  out="$(codesign "${SIGN_ARGS[@]}" "$1" 2>&1)" || { echo "$out" >&2; exit 1; }
}
n=0
while IFS= read -r -d '' f; do
  [ "$f" = "$MAIN_EXE" ] && continue
  if file -b "$f" | grep -q 'Mach-O'; then
    sign "$f"
    n=$((n + 1))
  fi
done < <(find "$APP/Contents" -depth -type f -print0)
sign "$APP"
codesign --verify --deep --strict "$APP"
if [ -n "$SIGN_KC" ]; then
  security lock-keychain "$SIGN_KC" 2>/dev/null || true
  codesign --verify --deep --strict \
    -R "=identifier \"$BUNDLE_ID\" and certificate leaf = H\"$CERT_SHA1\"" "$APP"
  echo "钉死的签名要求校验通过：identifier \"$BUNDLE_ID\" and certificate leaf = H\"$CERT_SHA1\""
fi
echo "已签名 $n 个内部 Mach-O + 外层 app；codesign --verify --deep --strict 通过"

# ---------------------------------------------------------------- dmg
DMG_SRC="$STAGE/dmg"
mkdir -p "$DMG_SRC"
mv "$APP" "$DMG_SRC/$APP_NAME.app"
ln -s /Applications "$DMG_SRC/Applications"
cat > "$DMG_SRC/先读我.txt" <<'EOF'
CC Remote Chat —— 先读我

① 先在「终端」里运行 claude 并登录一次（还没装的话，按 Claude Code 官方安装说明安装）。

② 把 CC Remote Chat 拖到旁边的「应用程序」，不要直接在这个窗口里双击。

③ 第一次打开如果提示无法验证开发者：打开「系统设置 → 隐私与安全性」，在最下面点「仍要打开」；
   或者在终端运行
       xattr -dr com.apple.quarantine "/Applications/CC Remote Chat.app"
   再打开。

④ claude 第一次读「文稿」「桌面」时，系统会问是否允许「CC Remote Chat」访问，点允许。

⑤ 关掉窗口后 claude 还在后台运行，下次打开会自动接上。

⑥ 更新到新版本：有新版时窗口上方会提示，点「立即更新」即可（第一次可能要在「系统设置 → 隐私与安全性 →
   App 管理」里允许 CC Remote Chat）；也可以手动把新的 CC Remote Chat 拖进「应用程序」选「替换」，再从
   「应用程序」打开（可能要再点一次「仍要打开」）。会话、设置、API key 都保留，正在跑的 claude 不受影响，
   新版会自动换掉旧版的后台。

（需要 Apple 芯片的 Mac，macOS 12 或更新。）
EOF
mkdir -p "$OUT_DIR"
DMG="$OUT_DIR/$APP_NAME $VERSION.dmg"
rm -f "$DMG"
echo "生成 dmg …"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$DMG_SRC" -fs HFS+ -format UDZO \
  -imagekey zlib-level=9 -ov "$DMG"
echo "完成：$DMG（$(du -h "$DMG" | cut -f1 | tr -d ' ')）"
