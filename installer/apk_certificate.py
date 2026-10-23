"""只接受 apksigner 完整验签后的单一证书 SHA-256，不相信包名或短签名摘要。"""
import re
import sys


def verify(output, expected):
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('未固定发布签名证书 SHA-256，停止安装')
    certificates = re.findall(r'^Signer #[0-9]+ certificate SHA-256 digest: ([a-f0-9]{64})$', output, re.M)
    if certificates != [expected]:
        raise ValueError('已安装 APK 的签名证书不符，停止且不覆盖')


if __name__ == '__main__':
    try:
        verify(sys.stdin.read(), sys.argv[1])
    except ValueError as exc:
        raise SystemExit(str(exc))
