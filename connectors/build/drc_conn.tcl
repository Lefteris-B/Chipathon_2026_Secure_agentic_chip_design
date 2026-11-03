crashbackups stop
drc euclidean on
foreach top {vss_conn vdd_conn} {
 gds read $top.gds
 load $top
 select top cell
 drc check
 drc catchup
 puts "DRC_RESULT $top: [drc list count total]"
}
quit -noprompt
