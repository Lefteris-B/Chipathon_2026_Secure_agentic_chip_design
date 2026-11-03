foreach cell {vss_conn vdd_conn} {
  gds read $cell.gds
  load $cell
  select top cell
  units internal
  set b [box values]
  property FIXED_BBOX "[lindex $b 0] [lindex $b 1] [lindex $b 2] [lindex $b 3]"
  puts "FIXED_BBOX $cell: [property list FIXED_BBOX]"
  gds write $cell.gds
}
quit -noprompt
