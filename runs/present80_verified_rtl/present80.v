// PRESENT-80 lightweight block cipher (encryption-only, iterative).
// Single flat module, bit-serial 7-pin interface per specs/present80.md.
//
//   Load  (144 cyc): shift {key[79..0], plaintext[63..0]} in MSB-first on din.
//   Encrypt(~33 cyc): 31 rounds (ARK, sBox, pLayer) + final ARK, one round/clk.
//   Unload  (64 cyc): shift ciphertext out MSB-first on dout while done & shift_out_en.
//
// Written function-free (S-box as an indexed constant LUT, sBoxLayer/pLayer as
// generate loops) so synthesis sees only single-driver combinational wires and
// one sequential process -- no Verilog-function temporaries for the linter to
// flag as "used but has no driver" pre-optimisation.
module present80 (
    input  wire clk,
    input  wire rst_n,         // asynchronous, active-low
    input  wire load_en,
    input  wire din,
    input  wire shift_out_en,
    output wire dout,
    output reg  done
);

    // ---- datapath registers -------------------------------------------------
    reg [79:0] key_reg;
    reg [63:0] state;

    // ---- control ------------------------------------------------------------
    localparam LOAD  = 2'd0;
    localparam ENC   = 2'd1;
    localparam FINAL = 2'd2;
    localparam DONE  = 2'd3;

    reg [1:0] phase;
    reg [7:0] load_cnt;    // 0..144
    reg [5:0] round;       // 1..31

    // ---- 4-bit S-box as a packed constant; SBOX[4*x +: 4] == S[x] ----------
    // S = C 5 6 B 9 0 A D 3 E F 8 4 7 1 2  (x = 0..F)
    localparam [63:0] SBOX = 64'h21748FE3DA09B65C;

    // ---- round datapath (combinational): addRoundKey -> sBox -> pLayer -----
    wire [63:0] round_ark = state ^ key_reg[79:16];   // addRoundKey (K_round)
    wire [63:0] round_sb;                             // sBoxLayer(round_ark)
    wire [63:0] round_out;                            // pLayer(round_sb)

    genvar n, gi;
    generate
        // sBoxLayer: independent S-box on each of the 16 nibbles
        for (n = 0; n < 16; n = n + 1) begin : SBOXL
            assign round_sb[4*n +: 4] = SBOX[4 * round_ark[4*n +: 4] +: 4];
        end
        // pLayer: bit gi -> position (16*gi) mod 63, and bit 63 -> 63
        for (gi = 0; gi < 63; gi = gi + 1) begin : PLAYER
            assign round_out[(16 * gi) % 63] = round_sb[gi];
        end
    endgenerate
    assign round_out[63] = round_sb[63];

    // ---- key schedule: rotate-left 61, S-box top nibble, XOR round counter -
    wire [79:0] key_rot  = (key_reg << 61) | (key_reg >> 19);
    wire [79:0] key_next = { SBOX[4 * key_rot[79:76] +: 4],  // [79:76] S-boxed
                             key_rot[75:20],                 // [75:20] unchanged
                             key_rot[19:15] ^ round[4:0],    // [19:15] XOR ctr
                             key_rot[14:0] };                // [14:0]  unchanged

    // ---- serial ciphertext output ------------------------------------------
    assign dout = (done & shift_out_en) ? state[63] : 1'b0;

    // ---- sequential control -------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            phase    <= LOAD;
            load_cnt <= 8'd0;
            round    <= 6'd0;
            done     <= 1'b0;
        end else begin
            case (phase)
                // -------- LOAD: shift 144 bits in, MSB-first ----------------
                LOAD: begin
                    done <= 1'b0;
                    if (load_en && load_cnt < 8'd144) begin
                        {key_reg, state} <= {key_reg[78:0], state[63:0], din};
                        load_cnt <= load_cnt + 8'd1;
                        if (load_cnt == 8'd143) begin  // 144th bit -> auto-start
                            phase <= ENC;
                            round <= 6'd1;
                        end
                    end
                end

                // -------- ENC: one round per clock, rounds 1..31 -----------
                ENC: begin
                    state   <= round_out;   // ARK -> sBox -> pLayer
                    key_reg <= key_next;     // advance to next round key
                    if (round == 6'd31)
                        phase <= FINAL;
                    else
                        round <= round + 6'd1;
                end

                // -------- FINAL: final addRoundKey with K_32 ---------------
                FINAL: begin
                    state <= state ^ key_reg[79:16];
                    done  <= 1'b1;
                    phase <= DONE;
                end

                // -------- DONE: hold result; unload or begin a fresh load --
                DONE: begin
                    if (load_en) begin            // a fresh load begins
                        {key_reg, state} <= {key_reg[78:0], state[63:0], din};
                        load_cnt <= 8'd1;
                        done     <= 1'b0;
                        phase    <= LOAD;
                    end else if (shift_out_en) begin
                        state <= {state[62:0], 1'b0}; // shift out MSB-first
                    end
                end
            endcase
        end
    end

endmodule
