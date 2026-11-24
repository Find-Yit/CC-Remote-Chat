#!/bin/bash
# dmg 发布签名密钥（自签名代码签名证书，docs/SPEC_V2_TUI.md §14.11）。
#
#   tools/release_key.sh init [--dir 目录] [--cn 名字]   生成密钥（目录里已有就拒绝，绝不覆盖）
#   tools/release_key.sh info [--dir 目录]               打印证书指纹（给 make_dmg.sh / 核对用）
#
# 默认目录 ~/.cc-remote-chat-release（0700）：
#   release.keychain-db   单独的钥匙串（只放这一把私钥；不加入钥匙串搜索列表、不碰登录钥匙串）
#   keychain-password     钥匙串密码（0600）；release-key.p12 也用它加密
#   release-key.p12       私钥 + 证书的备份（可在别的 Mac 上重新导入）
#   cert.pem              证书（公开信息）；fingerprints.txt：SHA-1 / SHA-256
#
# 为什么不用 ad-hoc：自动更新要验证「新版本确实是同一个人发的」（钉死证书指纹）；签名的指定要求稳定，
# macOS 的「文稿 / 桌面」等隐私授权在更新后保留。证书是自签名的（没有 Apple 开发者账号），Gatekeeper 照旧要
# 「仍要打开」一次。
#
# 不弹任何 GUI：所有 security 命令都显式带 -p / -k 密码，钥匙串创建后立即解锁；私钥的 ACL 只允许 /usr/bin/codesign，
# 分区列表 apple-tool:,apple:,codesign:（codesign 用它时不会弹授权框）。前后核对用户的钥匙串搜索列表与默认钥匙串不变。
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=C

CMD="${1:-}"; shift || true
DIR="$HOME/.cc-remote-chat-release"
CN="CC Remote Chat Release"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?}"; shift 2 ;;
    --cn) CN="${2:?}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac
KC="$DIR/release.keychain-db"
OPENSSL=/usr/bin/openssl

fp() {   # fp sha1|sha256 → 证书指纹（小写十六进制，无冒号）
  "$OPENSSL" x509 -in "$DIR/cert.pem" -noout -fingerprint "-$1" | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f'
}

snap() { printf '%s|%s' "$(security list-keychains -d user | tr -d ' \n')" "$(security default-keychain -d user | tr -d ' \n')"; }

case "$CMD" in
  init)
    if [ -e "$KC" ] || [ -e "$DIR/cert.pem" ] || [ -e "$DIR/release-key.p12" ]; then
      echo "发布密钥已存在：$DIR（不会覆盖；要看指纹用 tools/release_key.sh info）" >&2
      exit 1
    fi
    umask 077
    mkdir -p "$DIR"
    chmod 700 "$DIR"
    BEFORE="$(snap)"
    W="$(mktemp -d /tmp/ccr-relkey.XXXXXX)"
    trap 'rm -rf "$W"' EXIT
    echo "生成 RSA 3072 自签名代码签名证书（CN=$CN，10 年）…"
    "$OPENSSL" req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes -keyout "$W/key.pem" -out "$DIR/cert.pem" \
      -subj "/CN=$CN" -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature" \
      -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null
    CCR_KC_PW="$("$OPENSSL" rand -hex 32)"
    export CCR_KC_PW
    printf '%s\n' "$CCR_KC_PW" > "$DIR/keychain-password"
    chmod 600 "$DIR/keychain-password" "$DIR/cert.pem"
    "$OPENSSL" pkcs12 -export -inkey "$W/key.pem" -in "$DIR/cert.pem" -name "$CN" -out "$DIR/release-key.p12" \
      -passout env:CCR_KC_PW
    chmod 600 "$DIR/release-key.p12"
    rm -f "$W/key.pem"
    echo "创建单独的钥匙串 $KC …"
    security create-keychain -p "$CCR_KC_PW" "$KC"
    security set-keychain-settings "$KC"            # 不自动上锁（make_dmg.sh 每次签名前用密码解锁、签完上锁）
    security unlock-keychain -p "$CCR_KC_PW" "$KC"
    security import "$DIR/release-key.p12" -k "$KC" -P "$CCR_KC_PW" -T /usr/bin/codesign -f pkcs12 >/dev/null
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$CCR_KC_PW" "$KC" >/dev/null
    AFTER="$(snap)"
    if [ "$BEFORE" != "$AFTER" ]; then
      echo "!!! 钥匙串搜索列表 / 默认钥匙串变了（不应发生），请检查：" >&2
      echo "    之前：$BEFORE" >&2; echo "    之后：$AFTER" >&2
      exit 1
    fi
    SHA1="$(fp sha1)"; SHA256="$(fp sha256)"
    # 自检：在 /tmp 的小 bundle 上签一次并按钉死的要求校验（确认不会弹授权框、要求正确）
    T="$W/Self Test.app"
    mkdir -p "$T/Contents/MacOS"
    cp /usr/bin/true "$T/Contents/MacOS/Self Test"
    /usr/bin/plutil -create xml1 "$T/Contents/Info.plist"
    /usr/bin/plutil -insert CFBundleIdentifier -string local.cc-remote-chat.bundled "$T/Contents/Info.plist"
    /usr/bin/plutil -insert CFBundleExecutable -string "Self Test" "$T/Contents/Info.plist"
    codesign -f -s "$SHA1" --keychain "$KC" --timestamp=none "$T" 2>/dev/null
    codesign --verify --deep --strict -R "=identifier \"local.cc-remote-chat.bundled\" and certificate leaf = H\"$SHA1\"" "$T"
    security lock-keychain "$KC"
    printf 'SHA-1   %s\nSHA-256 %s\n' "$SHA1" "$SHA256" > "$DIR/fingerprints.txt"
    echo
    echo "发布密钥已生成：$DIR"
    echo "  证书 SHA-1  ：$SHA1"
    echo "  证书 SHA-256：$SHA256"
    echo "  钥匙串搜索列表与默认钥匙串未改变；自检签名 + 钉死要求校验通过。"
    echo
    echo "请备份这个目录：$DIR"
    echo "丢了以后发的更新朋友那边会验证失败，只能手动重装。"
    ;;
  info)
    [ -f "$DIR/cert.pem" ] || { echo "没有发布密钥：$DIR（先运行 tools/release_key.sh init）" >&2; exit 1; }
    echo "目录：$DIR"
    echo "SHA-1   $(fp sha1)"
    echo "SHA-256 $(fp sha256)"
    "$OPENSSL" x509 -in "$DIR/cert.pem" -noout -subject -enddate
    ;;
  *)
    sed -n '2,6p' "$0" >&2
    exit 2
    ;;
esac
