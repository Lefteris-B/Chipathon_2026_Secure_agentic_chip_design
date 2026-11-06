# Write one corner's SDF. Env: TAG (ss/tt/ff), PV (liberty corner), RC (nom/min/max).
set cells /foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0
read_lef $cells/techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef
read_lef $cells/lef/gf180mcu_fd_sc_mcu7t5v0.lef
read_lef connectors/lef/vss_conn.lef
read_lef connectors/lef/vdd_conn.lef
read_liberty $cells/lib/gf180mcu_fd_sc_mcu7t5v0__$::env(PV).lib
read_verilog runs/D10_D_605b/exports/D10_D.nl.v
link_design D10_D
read_sdc test/gl/D10_D.sdc
read_spef test/gl/out/$::env(RC).spef
puts "STA $::env(TAG) setup_ws=[sta::worst_slack -max] hold_ws=[sta::worst_slack -min]"
write_sdf -divider . test/gl/out/$::env(TAG).sdf
exit
