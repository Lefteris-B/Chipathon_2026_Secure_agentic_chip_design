#!/usr/bin/env bash
# Runs inside the image, from the repo root. See test/gl/README.md.
set -uo pipefail
O=test/gl/out
V=/foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog
PDK_LIB=$V/gf180mcu_fd_sc_mcu7t5v0.v
SIM_LIB=$O/lib_sim.v
NET=verilog/gl/D10_D.pnl.v
bad=0

openroad -no_splash -exit test/gl/rcx.tcl > $O/rcx.log 2>&1 || { echo "RCX failed: $O/rcx.log"; exit 2; }
while read -r tag pv rc; do
  TAG=$tag PV=$pv RC=$rc openroad -no_splash -exit test/gl/sdf.tcl 2>&1 | tee $O/sdf_$tag.log | grep '^STA' \
    || { echo "SDF failed: $O/sdf_$tag.log"; exit 2; }
  # iverilog reads the typical field, which write_sdf leaves empty: (a::b) -> (a:a:b).
  # Without this every delay silently stays at the library default.
  sed -i -E 's/\(([0-9.-]+)::([0-9.-]+)\)/(\1:\1:\2)/g' $O/$tag.sdf
done <<'CORNERS'
ss ss_125C_4v50 max
tt tt_025C_5v00 nom
ff ff_n40C_5v50 min
CORNERS
python3 test/gl/lib_sim.py "$PDK_LIB" "$SIM_LIB" || exit 2

# name half lib want(PASS|FAIL) flags...
run() {
  local name=$1 half=$2 lib=$3 want=$4; shift 4
  # A timescale must come first: otherwise the cells' delays compile as 1 second.
  echo '`timescale 1ns/1ps' > $O/ts.v
  iverilog -g2012 -o $O/$name.vvp -DUSE_POWER_PINS -DHALF=$half "$@" \
    $O/ts.v $V/primitives.v "$lib" test/gl/tb.v $NET -s tb 2> $O/$name.comp
  vvp $O/$name.vvp > $O/$name.log 2>&1
  local result unmatched got
  result=$(grep -E '^GL(PASS|FAIL)' $O/$name.log | tail -1)
  unmatched=$(grep -c 'Unable to match' $O/$name.log)
  got=${result:2:4}
  printf '%-14s %s unmatched_arcs=%s\n' "$name" "$result" "$unmatched"
  [[ "$got" == "$want" ]] || { echo "  UNEXPECTED: wanted $want"; bad=1; }
  [[ "$unmatched" == 0 ]] || { echo "  UNEXPECTED: unannotated cell arcs"; bad=1; }
}

declare -A delay
for half in 8 4; do
  ratio=$((half * 2))
  run zero_clk$ratio $half "$PDK_LIB" PASS -gno-specify -DFUNCTIONAL
  for c in ss tt ff; do
    run ${c}_clk$ratio $half "$SIM_LIB" PASS -gspecify -ginterconnect "-DSDF=\"$O/$c.sdf\""
    delay[$c]=$(sed -nE 's/^GLPASS.*done_clk2q=([0-9.]+)ns/\1/p' $O/${c}_clk$ratio.log)
  done
done
# Corners must really differ, or the SDF did not reach the simulation.
python3 -c "import sys; s,t,f=map(float,sys.argv[1:4]); sys.exit(0 if s>t>f>0 else 1)" \
  "${delay[ss]:-0}" "${delay[tt]:-0}" "${delay[ff]:-0}" \
  || { echo "UNEXPECTED: done delay not ordered ss>tt>ff: ${delay[ss]:-} ${delay[tt]:-} ${delay[ff]:-}"; bad=1; }

echo "-- injected faults, must FAIL"
run ctl_badexpect_ss 8 "$SIM_LIB" FAIL -gspecify -ginterconnect -DBAD_EXPECT "-DSDF=\"$O/ss.sdf\""
run ctl_mosistuck_ss 8 "$SIM_LIB" FAIL -gspecify -ginterconnect -DMOSI_STUCK "-DSDF=\"$O/ss.sdf\""
run ctl_mosistuck_ff 4 "$SIM_LIB" FAIL -gspecify -ginterconnect -DMOSI_STUCK "-DSDF=\"$O/ff.sdf\""

[[ $bad == 0 ]] && echo "T1.3 PASS" || echo "T1.3 FAIL"
exit $bad
