#!/bin/zsh
# 이 맥의 CPU·GPU·메모리·배터리를 한 줄 JSON으로 낸다. 관리자 권한 없이 동작한다.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
# top은 1회에 CPU를 1.3초 써서 쓰지 않는다. ps는 0.02초다.
cpu=$(ps -A -o %cpu | awk -v n=$(sysctl -n hw.logicalcpu) 'NR>1{s+=$1} END{printf "%.0f", s/n}')
gpu=$(ioreg -r -d 1 -c IOAccelerator | sed -n 's/.*"Device Utilization %"=\([0-9]*\).*/\1/p' | head -1)
vm_stat | awk -v total=$(sysctl -n hw.memsize) -v cpu="$cpu" -v gpu="${gpu:-0}" -v batt="$(pmset -g batt | tr -d '\n')" '
  /page size of/ {page=$8}
  /Anonymous pages/ {a=$3}
  /Pages purgeable/ {p=$3}
  /Pages wired/ {w=$4}
  /occupied by compressor/ {c=$5}
  END {
    pct="null"; if (match(batt,/[0-9]+%/)) pct=substr(batt,RSTART,RLENGTH-1)
    ac=(batt ~ /AC Power/)?"true":"false"
    printf "{\"cpu\":%.0f,\"gpu\":%d,\"mem\":%.1f,\"total\":%.0f,\"batt\":%s,\"ac\":%s}\n",
      cpu, gpu, (a-p+w+c)*page/2^30, total/2^30, pct, ac
  }'
