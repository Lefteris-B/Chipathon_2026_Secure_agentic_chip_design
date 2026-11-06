# Extract SPEF from the shipped routed DEF at the three RC corners.
set cells /foss/pdks/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0
read_lef $cells/techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef
read_lef $cells/lef/gf180mcu_fd_sc_mcu7t5v0.lef
read_lef connectors/lef/vss_conn.lef
read_lef connectors/lef/vdd_conn.lef
read_def runs/D10_D_605b/exports/D10_D.def
foreach rc {nom min max} {
  define_process_corner -ext_model_index 0 X
  extract_parasitics -ext_model_file /foss/pdks/gf180mcuD/libs.tech/librelane/rules.openrcx.gf180mcuD.$rc \
    -corner_cnt 1 -max_res 50 -coupling_threshold 0.1 -cc_model 10 -context_depth 5
  write_spef test/gl/out/$rc.spef
}
exit
