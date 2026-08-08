module present80 #(
    parameter BLOCK_BITS   = 64,
    parameter CLK_PERIOD_NS = 10,
    parameter ENCRYPT_CYCLES = 32,
    parameter KEY_BITS     = 80,
    parameter LOAD_CYCLES  = 144,
    parameter ROUNDS       = 31,
    parameter UNLOAD_CYCLES = 64
)(
    input  wire clk,
    input  wire rst_n,
    input  wire load_en,
    input  wire din,
    input  wire shift_out_en,
    output wire dout,
    output reg  done
);

// ============================================================
// FSM states
// ============================================================
localparam ST_IDLE    = 2'd0;
localparam ST_LOAD    = 2'd1;
localparam ST_ENCRYPT = 2'd2;
localparam ST_DONE    = 2'd3;

reg [1:0] state, next_state;

// ============================================================
// Registers
// ============================================================
reg [KEY_BITS-1:0]   key_reg;
reg [BLOCK_BITS-1:0] block_reg;
reg [7:0]            load_cnt;   // counts 0..143
reg [4:0]            round_cnt;  // counts 0..30 (31 rounds)

// Serial output shift register
reg [BLOCK_BITS-1:0] out_shift;

// ============================================================
// S-box (4-bit, PRESENT specification)
// ============================================================
function [3:0] sbox;
    input [3:0] x;
    case (x)
        4'h0: sbox = 4'hC;
        4'h1: sbox = 4'h5;
        4'h2: sbox = 4'h6;
        4'h3: sbox = 4'hB;
        4'h4: sbox = 4'h9;
        4'h5: sbox = 4'h0;
        4'h6: sbox = 4'hA;
        4'h7: sbox = 4'hD;
        4'h8: sbox = 4'h3;
        4'h9: sbox = 4'hE;
        4'hA: sbox = 4'hF;
        4'hB: sbox = 4'h8;
        4'hC: sbox = 4'h4;
        4'hD: sbox = 4'h7;
        4'hE: sbox = 4'h1;
        4'hF: sbox = 4'h2;
        default: sbox = 4'h0;
    endcase
endfunction

// ============================================================
// S-box layer: apply sbox to all 16 nibbles
// ============================================================
function [63:0] sbox_layer;
    input [63:0] x;
    integer i;
    begin
        for (i = 0; i < 16; i = i + 1)
            sbox_layer[i*4 +: 4] = sbox(x[i*4 +: 4]);
    end
endfunction

// ============================================================
// pLayer: bit permutation P(i) = (i*16) mod 63, P(63)=63
// ============================================================
function [63:0] player;
    input [63:0] x;
    integer i;
    reg [63:0] y;
    begin
        for (i = 0; i < 63; i = i + 1)
            y[(i*16) % 63] = x[i];
        y[63] = x[63];
        player = y;
    end
endfunction

// ============================================================
// Key schedule: generate round key and update key register
// PRESENT-80 key schedule:
//   1. Rotate key left by 61 bits (equiv right by 19)
//   2. Apply sbox to top 4 bits
//   3. XOR bits [19:15] with round counter
// Returns {new_key, round_key} where round_key = key[79:16]
// Actually round key for round i is top 64 bits of key after update
// ============================================================
// Round key extraction: top 64 bits of key register
function [63:0] get_round_key;
    input [KEY_BITS-1:0] k;
    begin
        get_round_key = k[KEY_BITS-1 : KEY_BITS-BLOCK_BITS];
    end
endfunction

// Key update function
function [KEY_BITS-1:0] key_update;
    input [KEY_BITS-1:0] k;
    input [4:0]          round_num; // 1-based round number for XOR
    reg [KEY_BITS-1:0] k2;
    begin
        // Rotate left by 61 (= rotate right by 19)
        k2 = {k[18:0], k[KEY_BITS-1:19]};
        // Apply sbox to top nibble
        k2[KEY_BITS-1 : KEY_BITS-4] = sbox(k2[KEY_BITS-1 : KEY_BITS-4]);
        // XOR bits [19:15] with round counter (5 bits)
        k2[19:15] = k2[19:15] ^ round_num;
        key_update = k2;
    end
endfunction

// ============================================================
// Combinational encryption round
// ============================================================
function [63:0] enc_round;
    input [63:0] blk;
    input [63:0] rk;
    begin
        enc_round = player(sbox_layer(blk ^ rk));
    end
endfunction

// ============================================================
// FSM: sequential
// ============================================================
always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
        state <= ST_IDLE;
    else
        state <= next_state;
end

// ============================================================
// FSM: next state logic
// ============================================================
always @(*) begin
    case (state)
        ST_IDLE:    next_state = load_en ? ST_LOAD : ST_IDLE;
        ST_LOAD:    next_state = (!load_en && load_cnt == (LOAD_CYCLES-1)) ? ST_ENCRYPT : ST_LOAD;
        ST_ENCRYPT: next_state = (round_cnt == ROUNDS) ? ST_DONE : ST_ENCRYPT;
        ST_DONE:    next_state = load_en ? ST_LOAD : ST_DONE;
        default:    next_state = ST_IDLE;
    endcase
end

// ============================================================
// Load counter
// ============================================================
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        load_cnt <= 8'd0;
    end else begin
        case (state)
            ST_IDLE: begin
                if (load_en)
                    load_cnt <= 8'd1;
                else
                    load_cnt <= 8'd0;
            end
            ST_LOAD: begin
                if (load_en) begin
                    if (load_cnt == (LOAD_CYCLES-1))
                        load_cnt <= 8'd0;
                    else
                        load_cnt <= load_cnt + 8'd1;
                end else begin
                    // load_en deasserted
                    load_cnt <= load_cnt; // hold, transition handled by FSM
                end
            end
            default: load_cnt <= 8'd0;
        endcase
    end
end

// ============================================================
// Serial load: shift key then plaintext MSB-first
// First 80 bits = key, next 64 bits = plaintext
// ============================================================
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        key_reg   <= {KEY_BITS{1'b0}};
        block_reg <= {BLOCK_BITS{1'b0}};
    end else begin
        if ((state == ST_IDLE && load_en) || state == ST_LOAD) begin
            if (load_en) begin
                // Shift in MSB-first
                // load_cnt tells us which bit we're loading (0-indexed)
                // Bits 0..79 go into key_reg, bits 80..143 go into block_reg
                if (load_cnt < KEY_BITS) begin
                    key_reg <= {key_reg[KEY_BITS-2:0], din};
                end else begin
                    block_reg <= {block_reg[BLOCK_BITS-2:0], din};
                end
            end
        end else if (state == ST_DONE && load_en) begin
            // Starting new load
            if (load_cnt < KEY_BITS)
                key_reg <= {key_reg[KEY_BITS-2:0], din};
            else
                block_reg <= {block_reg[BLOCK_BITS-2:0], din};
        end
    end
end

// ============================================================
// Round counter and encryption
// ============================================================
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        round_cnt <= 5'd0;
    end else begin
        if (state == ST_ENCRYPT) begin
            if (round_cnt < ROUNDS)
                round_cnt <= round_cnt + 5'd1;
        end else begin
            round_cnt <= 5'd0;
        end
    end
end

// Encryption datapath
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        // nothing extra needed
    end else begin
        if (state == ST_ENCRYPT && round_cnt < ROUNDS) begin
            // Apply round: AddRoundKey, SboxLayer, pLayer
            // Round number for key schedule XOR is round_cnt+1 (1-based)
            block_reg <= enc_round(block_reg, get_round_key(key_reg));
            key_reg   <= key_update(key_reg, round_cnt + 5'd1);
        end
    end
end

// ============================================================
// Done signal and output shift register
// ============================================================
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        done      <= 1'b0;
        out_shift <= {BLOCK_BITS{1'b0}};
    end else begin
        if (next_state == ST_DONE && state == ST_ENCRYPT) begin
            // Final round: add last round key (no sbox/player for last step)
            // PRESENT: after 31 rounds of (AddRoundKey+SBox+pLayer), do final AddRoundKey
            done      <= 1'b1;
            out_shift <= block_reg ^ get_round_key(key_reg);
        end else if (state == ST_DONE) begin
            if (load_en) begin
                done <= 1'b0;
            end else if (shift_out_en) begin
                out_shift <= {out_shift[BLOCK_BITS-2:0], 1'b0};
            end
        end else begin
            done <= 1'b0;
        end
    end
end

// ============================================================
// Serial output
// ============================================================
assign dout = out_shift[BLOCK_BITS-1];

endmodule
