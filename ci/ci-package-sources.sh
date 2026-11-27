#!/usr/bin/env bash
# 国内 GitLab Runner 的软件包源。

export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

use_ci_apt_mirrors() {
  local sources_file
  for sources_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    if [[ -f "$sources_file" ]]; then
      # 同时支持 Ubuntu 和 Debian 的 list/deb822 格式，只替换 URL，保留发行版、组件和签名配置。
      sed -Ei \
        -e 's#https?://(archive|security)\.ubuntu\.com/ubuntu([/[:space:]]|$)#https://mirrors.aliyun.com/ubuntu\2#g' \
        -e 's#https?://download\.docker\.com/linux/ubuntu([/[:space:]]|$)#https://mirrors.aliyun.com/docker-ce/linux/ubuntu\1#g' \
        -e 's#https?://deb\.debian\.org/debian-security([/[:space:]]|$)#https://mirrors.aliyun.com/debian-security\1#g' \
        -e 's#https?://security\.debian\.org(/debian-security)?([/[:space:]]|$)#https://mirrors.aliyun.com/debian-security\2#g' \
        -e 's#https?://(deb|ftp)\.debian\.org/debian([/[:space:]]|$)#https://mirrors.aliyun.com/debian\2#g' \
        "$sources_file" || return
    fi
  done
}
