set -eu
export PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
if [ "$GAME" = apk ]; then
  sed -i -e 's|http://deb.debian.org|https://mirrors.aliyun.com|g' -e 's|https://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
  apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update
  apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 install -y --no-install-recommends android-sdk-build-tools default-jre-headless
  export ANDROID_HOME=/usr/lib/android-sdk
fi
python -m pip install --disable-pip-version-check --timeout 30 --retries 2 --index-url https://mirrors.aliyun.com/pypi/simple 'boto3>=1.40,<2' 'paramiko>=5,<6' 'requests>=2.32,<3'
if [ "$GAME" = apk ]; then
  if [ -n "${SOURCE_RUN_ID:-}" ]; then
    python -c 'import os; from publisher.yunxiao import source_workflow; source_workflow("apk", int(os.environ["SOURCE_RUN_ID"]), os.environ["PUBLISHER_SHA"])'
  fi
  python -m publisher.apk "$RELEASE_ID" --execute &
  apk_pid=$!
  trap 'kill "$apk_pid" 2>/dev/null || true' EXIT
  while kill -0 "$apk_pid" 2>/dev/null; do
    if [ -d work/apk ]; then
      date -u '+%H:%M:%S UTC APK download bytes'
      find work/apk -maxdepth 1 -type f -name '*.apk' -printf '%f: %s bytes\n'
    fi
    sleep 15
  done
  wait "$apk_pid"
else
  python -m publisher.yunxiao upload "$GAME" --run-id "$SOURCE_RUN_ID" --attempt "$PUBLICATION_ATTEMPT"
fi
