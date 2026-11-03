// D10_D — chipathon 2026 top-level macro for team D10, variant D.
// SPI-slave register-file variant (replaces the PRESENT-80 core; export-control pivot).
//
// Exposes exactly the 35 signal pins of the organizer-supplied DEF template
// `D10_D.def` (VDD/VSS are power, added by LibreLane's PDN step). The pad
// wiring below is IDENTICAL to the PRESENT-80 wrapper — only the core
// instance and its port mapping change, because the template pin names are
// fixed and this design was chosen to map onto them one-for-one:
//
//     template pin      core port    role
//     ------------      ---------    ----
//     clk               clk          system clock (only clock domain)
//     rst_n             rst_n        async active-low reset
//     load_en           sclk         SPI clock  -- SAMPLED, not a clock
//     shift_out_en      cs_n         chip select, active low
//     din               mosi         serial data in
//     dout              miso         serial data out
//     done              done         transaction complete (level)
//
// Pad-control terminal constants follow gf180mcu_fd_io.v, unchanged:
//   in_c / in_s inputs: PU/PD tied 0 (no pull).
//   bi_t (dout, done) as OUTPUTS: OE=1, IE=0, PU=PD=0, PDRV0=PDRV1=1,
//   SL=0, CS=0, A=core signal, Y (=*_IN) unused.
module D10_D (
    // clk pad (in_s): signal in + pull enables out
    input  wire clk,
    output wire clk_PU,
    output wire clk_PD,
    // rst_n pad (in_c)
    input  wire rst_n,
    output wire rst_n_PU,
    output wire rst_n_PD,
    // din pad (in_c) -> mosi
    input  wire din,
    output wire din_PU,
    output wire din_PD,
    // load_en pad (in_c) -> sclk
    input  wire load_en,
    output wire load_en_PU,
    output wire load_en_PD,
    // shift_out_en pad (in_c) -> cs_n
    input  wire shift_out_en,
    output wire shift_out_en_PU,
    output wire shift_out_en_PD,
    // dout pad (bi_t) -> miso, output mode
    output wire dout_CS,
    output wire dout_SL,
    output wire dout_IE,
    output wire dout_OE,
    output wire dout_PU,
    output wire dout_PD,
    output wire dout_OUT,
    output wire dout_PDRV0,
    output wire dout_PDRV1,
    input  wire dout_IN,
    // done pad (bi_t) — output mode
    output wire done_CS,
    output wire done_SL,
    output wire done_IE,
    output wire done_OE,
    output wire done_PU,
    output wire done_PD,
    output wire done_OUT,
    output wire done_PDRV0,
    output wire done_PDRV1,
    input  wire done_IN
);

    // ---- core -------------------------------------------------------------
    wire core_miso;
    wire core_done;

    spi_slave_regs u_core (
        .clk   (clk),
        .rst_n (rst_n),
        .sclk  (load_en),        // template pin load_en carries SPI SCLK
        .cs_n  (shift_out_en),   // template pin shift_out_en carries CS_n
        .mosi  (din),
        .miso  (core_miso),
        .done  (core_done)
    );

    // ---- input pads: no pull ---------------------------------------------
    assign clk_PU          = 1'b0;
    assign clk_PD          = 1'b0;
    assign rst_n_PU        = 1'b0;
    assign rst_n_PD        = 1'b0;
    assign din_PU          = 1'b0;
    assign din_PD          = 1'b0;
    assign load_en_PU      = 1'b0;
    assign load_en_PD      = 1'b0;
    assign shift_out_en_PU = 1'b0;
    assign shift_out_en_PD = 1'b0;

    // ---- dout pad (miso): output, full drive ------------------------------
    assign dout_OUT   = core_miso;
    assign dout_OE    = 1'b1;
    assign dout_IE    = 1'b0;
    assign dout_PU    = 1'b0;
    assign dout_PD    = 1'b0;
    assign dout_PDRV0 = 1'b1;
    assign dout_PDRV1 = 1'b1;
    assign dout_SL    = 1'b0;
    assign dout_CS    = 1'b0;

    // ---- done pad: output, full drive ------------------------------------
    assign done_OUT   = core_done;
    assign done_OE    = 1'b1;
    assign done_IE    = 1'b0;
    assign done_PU    = 1'b0;
    assign done_PD    = 1'b0;
    assign done_PDRV0 = 1'b1;
    assign done_PDRV1 = 1'b1;
    assign done_SL    = 1'b0;
    assign done_CS    = 1'b0;

    // dout_IN / done_IN are the pad input-return paths (Y); unused in
    // output-only operation. Referenced here so lint/synth see a driver-free
    // read rather than a dangling port.
    wire _unused_pad_in = &{1'b0, dout_IN, done_IN};

    // Power-connector bridge macros: pad Metal2 VSS/VDD -> Via2 -> Metal3, then
    // the net-aware macro PDN grid vias Metal3->Metal4/5. Left UNconnected in
    // RTL on purpose — a .VSS(VSS) tie makes a separate regular net that never
    // merges with the SPECIAL power net (LVS VSS_uq0 island). The PDN cfg's
    // add_global_connection ties these pins straight to the power nets instead.
    // (* keep *) + single-pass synth (--rtl-to-physical) preserves the empties.
    (* keep *) vss_conn vss_conn_0 ();
    (* keep *) vdd_conn vdd_conn_0 ();

endmodule

(* blackbox *)
module vss_conn (inout VSS);
endmodule

(* blackbox *)
module vdd_conn (inout VDD);
endmodule

// ============================================================================
// Verified `spi_slave_regs` core, from NoeSI run spi_slave_regs_r3
// (rtl v2, status=accepted). Passed the F19.14 SPI vector gate: 17
// transactions / 13 checked reads, done asserted on every frame.
// Inner-loop synth-strict check (F-SYNTHELAB) clean on the FIRST RTL:
// `proc; check` -> 0 problems, so LibreLane's Checker.YosysSynthChecks
// has nothing to reject. SIGNOFF: STA 0 setup / 0 hold, DRC / LVS /
// security all gate_ok; 249 cells, 0 inferred latches.
// ============================================================================

module spi_slave_regs #(
    parameter [6:0] ADDR_CTRL = 7'h02,
    parameter [6:0] ADDR_ID   = 7'h00,
    parameter [6:0] ADDR_SCR0 = 7'h10,
    parameter [6:0] ADDR_SCR1 = 7'h11,
    parameter [6:0] ADDR_SCR2 = 7'h12,
    parameter [6:0] ADDR_SCR3 = 7'h13,
    parameter [6:0] ADDR_VER  = 7'h01,
    parameter integer CLK_PERIOD_NS = 10,
    parameter [7:0] ID_VAL      = 8'h5A,
    parameter [7:0] VERSION_VAL = 8'h01
)(
    input  wire clk,
    input  wire rst_n,
    input  wire sclk,
    input  wire cs_n,
    input  wire mosi,
    output reg  miso,
    output reg  done
);

    reg [2:0] sclk_sync;
    reg [2:0] cs_sync;
    reg [1:0] mosi_sync;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sclk_sync <= 3'b000;
            cs_sync   <= 3'b111;
            mosi_sync <= 2'b00;
        end else begin
            sclk_sync <= {sclk_sync[1:0], sclk};
            cs_sync   <= {cs_sync[1:0],   cs_n};
            mosi_sync <= {mosi_sync[0],   mosi};
        end
    end

    wire mosi_s    = mosi_sync[1];
    wire sclk_rise = (sclk_sync[2:1] == 2'b01);
    wire sclk_fall = (sclk_sync[2:1] == 2'b10);
    wire cs_assert = (cs_sync[2:1] == 2'b10);
    wire cs_deassert = (cs_sync[2:1] == 2'b01);

    reg        frame_active;
    reg [4:0]  bit_cnt;
    reg [15:0] shift_in;
    reg [7:0]  shift_out;
    reg [6:0]  addr_lat;
    reg        rw_lat;

    reg [7:0] ctrl_reg;
    reg [7:0] scr0_reg;
    reg [7:0] scr1_reg;
    reg [7:0] scr2_reg;
    reg [7:0] scr3_reg;

    reg [7:0] rd_data;
    always @(*) begin
        case (addr_lat)
            ADDR_ID:   rd_data = ID_VAL;
            ADDR_VER:  rd_data = VERSION_VAL;
            ADDR_CTRL: rd_data = ctrl_reg;
            ADDR_SCR0: rd_data = scr0_reg;
            ADDR_SCR1: rd_data = scr1_reg;
            ADDR_SCR2: rd_data = scr2_reg;
            ADDR_SCR3: rd_data = scr3_reg;
            default:   rd_data = 8'h00;
        endcase
    end

    // Command byte (bit15..bit8) as it will be after this rising edge
    wire [7:0] cmd_byte  = {shift_in[6:0], mosi_s};
    wire [7:0] data_byte = {shift_in[6:0], mosi_s};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            frame_active <= 1'b0;
            bit_cnt      <= 5'd0;
            shift_in     <= 16'd0;
            shift_out    <= 8'd0;
            addr_lat     <= 7'd0;
            rw_lat       <= 1'b0;
            miso         <= 1'b0;
            done         <= 1'b0;
            ctrl_reg     <= 8'd0;
            scr0_reg     <= 8'd0;
            scr1_reg     <= 8'd0;
            scr2_reg     <= 8'd0;
            scr3_reg     <= 8'd0;
        end else begin
            // Frame-active flag from cs edges only
            if (cs_assert) begin
                frame_active <= 1'b1;
                bit_cnt      <= 5'd0;
                done         <= 1'b0;
                miso         <= 1'b0;
            end else if (cs_deassert) begin
                frame_active <= 1'b0;
                bit_cnt      <= 5'd0;
                miso         <= 1'b0;
            end else if (frame_active) begin
                if (sclk_rise) begin
                    shift_in <= {shift_in[14:0], mosi_s};

                    if (bit_cnt == 5'd7) begin
                        rw_lat   <= cmd_byte[7];
                        addr_lat <= cmd_byte[6:0];
                    end

                    if (bit_cnt == 5'd15) begin
                        done    <= 1'b1;
                        bit_cnt <= 5'd0;
                        if (rw_lat) begin
                            case (addr_lat)
                                ADDR_CTRL: ctrl_reg <= data_byte;
                                ADDR_SCR0: scr0_reg <= data_byte;
                                ADDR_SCR1: scr1_reg <= data_byte;
                                ADDR_SCR2: scr2_reg <= data_byte;
                                ADDR_SCR3: scr3_reg <= data_byte;
                                default: ;
                            endcase
                        end
                    end else begin
                        bit_cnt <= bit_cnt + 5'd1;
                        // extra edges beyond 16 clear done
                        if (bit_cnt == 5'd0 && done) begin
                            done <= 1'b0;
                        end
                    end
                end

                if (sclk_fall) begin
                    if (rw_lat) begin
                        miso <= 1'b0;
                    end else if (bit_cnt == 5'd8) begin
                        miso      <= rd_data[7];
                        shift_out <= {rd_data[6:0], 1'b0};
                    end else if (bit_cnt > 5'd8) begin
                        miso      <= shift_out[7];
                        shift_out <= {shift_out[6:0], 1'b0};
                    end else begin
                        miso <= 1'b0;
                    end
                end
            end
        end
    end

endmodule
