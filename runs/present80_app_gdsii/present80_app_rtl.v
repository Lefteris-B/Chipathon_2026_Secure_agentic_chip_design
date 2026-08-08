module present80 #(
    parameter BLOCK_BITS  = 64,
    parameter ENC_CYCLES  = 32,
    parameter KEY_BITS    = 80,
    parameter LOAD_CYCLES = 144,
    parameter ROUNDS      = 31,
    parameter UNLOAD_CYCLES = 64
)(
    input  wire clk,
    input  wire rst_n,
    input  wire load_en,
    input  wire din,
    input  wire shift_out_en,
    output wire dout,
    output wire done
);

localparam ST_IDLE    = 3'd0;
localparam ST_LOAD    = 3'd1;
localparam ST_ENC     = 3'd2;
localparam ST_DONE    = 3'd3;
localparam ST_UNLOAD  = 3'd4;

reg [2:0] state, next_state;

reg [KEY_BITS-1:0]   key_reg;
reg [BLOCK_BITS-1:0] block_reg;
reg [7:0]            load_cnt;
reg [4:0]            round_cnt;
reg [6:0]            unload_cnt;
reg                  done_reg;
reg [BLOCK_BITS-1:0] cipher_shift;

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

function [63:0] sLayer;
    input [63:0] b;
    integer i;
    reg [63:0] tmp;
    begin
        for (i = 0; i < 16; i = i + 1)
            tmp[4*i +: 4] = sbox(b[4*i +: 4]);
        sLayer = tmp;
    end
endfunction

function [63:0] pLayer;
    input [63:0] b;
    integer i;
    reg [63:0] tmp;
    begin
        tmp[63] = b[63];
        for (i = 0; i < 63; i = i + 1) begin
            tmp[(i * 16) % 63] = b[i];
        end
        pLayer = tmp;
    end
endfunction

function [79:0] key_schedule;
    input [79:0] k;
    input [4:0]  round_num;
    reg [79:0]   tmp;
    begin
        tmp = {k[18:0], k[79:19]};
        tmp[79:76] = sbox(tmp[79:76]);
        tmp[19:15] = tmp[19:15] ^ round_num;
        key_schedule = tmp;
    end
endfunction

wire [63:0] block_after_xor;
wire [63:0] block_after_sbox;
wire [63:0] block_after_perm;

assign block_after_xor  = block_reg ^ key_reg[79:16];
assign block_after_sbox = sLayer(block_after_xor);
assign block_after_perm = pLayer(block_after_sbox);

always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
        state <= ST_IDLE;
    else
        state <= next_state;
end

always @(*) begin
    case (state)
        ST_IDLE:   next_state = load_en ? ST_LOAD : ST_IDLE;
        ST_LOAD:   next_state = (load_cnt == LOAD_CYCLES-1) ? ST_ENC : ST_LOAD;
        ST_ENC:    next_state = (round_cnt == ROUNDS) ? ST_DONE : ST_ENC;
        ST_DONE:   next_state = shift_out_en ? ST_UNLOAD : ST_DONE;
        ST_UNLOAD: next_state = (unload_cnt == UNLOAD_CYCLES-1) ? ST_IDLE : ST_UNLOAD;
        default:   next_state = ST_IDLE;
    endcase
end

always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        key_reg      <= {KEY_BITS{1'b0}};
        block_reg    <= {BLOCK_BITS{1'b0}};
        load_cnt     <= 8'd0;
        round_cnt    <= 5'd0;
        unload_cnt   <= 7'd0;
        done_reg     <= 1'b0;
        cipher_shift <= {BLOCK_BITS{1'b0}};
    end else begin
        case (state)
            ST_IDLE: begin
                load_cnt   <= 8'd0;
                round_cnt  <= 5'd0;
                unload_cnt <= 7'd0;
                done_reg   <= 1'b0;
                if (load_en) begin
                    key_reg  <= {key_reg[KEY_BITS-2:0], din};
                    load_cnt <= 8'd1;
                end
            end

            ST_LOAD: begin
                if (load_cnt < KEY_BITS) begin
                    key_reg <= {key_reg[KEY_BITS-2:0], din};
                end else begin
                    block_reg <= {block_reg[BLOCK_BITS-2:0], din};
                end
                if (load_cnt == LOAD_CYCLES-1) begin
                    load_cnt  <= 8'd0;
                    round_cnt <= 5'd0;
                end else begin
                    load_cnt <= load_cnt + 8'd1;
                end
            end

            ST_ENC: begin
                if (round_cnt == ROUNDS) begin
                    // Final addRoundKey only (32nd cycle)
                    block_reg    <= block_reg ^ key_reg[79:16];
                    done_reg     <= 1'b1;
                    cipher_shift <= block_reg ^ key_reg[79:16];
                    round_cnt    <= 5'd0;
                end else begin
                    // Full round: ARK + sbox + pLayer + key_schedule
                    block_reg <= block_after_perm;
                    key_reg   <= key_schedule(key_reg, round_cnt + 5'd1);
                    round_cnt <= round_cnt + 5'd1;
                end
            end

            ST_DONE: begin
                done_reg     <= 1'b1;
                cipher_shift <= block_reg;
                if (shift_out_en) begin
                    unload_cnt   <= 7'd0;
                    cipher_shift <= {block_reg[BLOCK_BITS-2:0], 1'b0};
                end
            end

            ST_UNLOAD: begin
                if (unload_cnt == UNLOAD_CYCLES-1) begin
                    unload_cnt <= 7'd0;
                    done_reg   <= 1'b0;
                end else begin
                    cipher_shift <= {cipher_shift[BLOCK_BITS-2:0], 1'b0};
                    unload_cnt   <= unload_cnt + 7'd1;
                end
            end

            default: begin
                done_reg <= 1'b0;
            end
        endcase
    end
end

assign done = done_reg;
assign dout = cipher_shift[BLOCK_BITS-1];

endmodule
