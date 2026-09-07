#!/bin/sh
set -e

DATA_DIR="${APEX_DATA:-/data}"
IMAGE_DIR="${APEX_IMAGE:-/image}"

# 宿主机挂载目录通常属 root，容器内非 root 用户无法写入 SQLite 文件。
# 这里以 root 修正归属后再降权运行，bind mount 与命名卷都能开箱可用。
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$IMAGE_DIR"
    chown -R appuser:appuser "$DATA_DIR" "$IMAGE_DIR"
    exec gosu appuser "$@"
fi

exec "$@"
