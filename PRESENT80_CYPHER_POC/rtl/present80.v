module present80 #(
    parameter BLOCK_WIDTH  = 64,
    parameter CLK_PERIOD_NS = 10,
    parameter KEY_WIDTH    = 80,
    parameter ROUNDS       = 31
)(
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  start,
    input  wire [BLOCK_WIDTH-1:0] plaintext,
    input  wire [KEY_WIDTH-1:0]   key,
    output reg  [BLOCK_WIDTH-1:0] ciphertext,
    output reg                    done
);

    // -----------------------------------------------------------------------
    // PRESENT-80 S-Box (4-bit -> 4-bit)
    // -----------------------------------------------------------------------
    function automatic [3:0] sbox;
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

    // -----------------------------------------------------------------------
    // S-Layer: apply S-Box to all 16 nibbles
    // -----------------------------------------------------------------------
    function automatic [63:0] sLayer;
        input [63:0] state;
        integer i;
        reg [63:0] result;
        begin
            for (i = 0; i < 16; i = i + 1)
                result[4*i +: 4] = sbox(state[4*i +: 4]);
            sLayer = result;
        end
    endfunction

    // -----------------------------------------------------------------------
    // P-Layer: bit permutation P(i) = (i/4) + 16*(i mod 4)
    // -----------------------------------------------------------------------
    function automatic [63:0] pLayer;
        input [63:0] state;
        integer i;
        reg [63:0] result;
        begin
            for (i = 0; i < 64; i = i + 1)
                result[(i/4) + 16*(i%4)] = state[i];
            pLayer = result;
        end
    endfunction

    // -----------------------------------------------------------------------
    // Key schedule: generate round key K_{round_counter+1} from current key
    // The key register is updated each round.
    // Key schedule for PRESENT-80:
    //   1. Rotate key register left by 61 bits (or right by 19 bits)
    //   2. Pass top 4 bits through S-Box
    //   3. XOR bits [19:15] with round counter
    // -----------------------------------------------------------------------
    function automatic [KEY_WIDTH-1:0] keySchedule;
        input [KEY_WIDTH-1:0] k;
        input [4:0]           round_cnt; // 1..31
        reg [KEY_WIDTH-1:0]   k1;
        reg [3:0]             s_out;
        begin
            // Step 1: rotate left by 61 (= rotate right by 19)
            k1 = {k[18:0], k[79:19]};
            // Step 2: S-Box on top 4 bits [79:76]
            s_out = sbox(k1[79:76]);
            k1[79:76] = s_out;
            // Step 3: XOR bits [19:15] with round counter
            k1[19:15] = k1[19:15] ^ round_cnt;
            keySchedule = k1;
        end
    endfunction

    // -----------------------------------------------------------------------
    // State registers
    // -----------------------------------------------------------------------
    reg [BLOCK_WIDTH-1:0] state_reg;
    reg [KEY_WIDTH-1:0]   key_reg;
    reg [4:0]             round_cnt;   // counts 0..31
    reg                   running;

    // -----------------------------------------------------------------------
    // Round logic (combinational)
    // -----------------------------------------------------------------------
    wire [63:0] after_xor;
    wire [63:0] after_sbox;
    wire [63:0] after_perm;
    wire [79:0] next_key;

    // AddRoundKey: XOR state with top 64 bits of key
    assign after_xor  = state_reg ^ key_reg[79:16];

    // S-Layer
    assign after_sbox = sLayer(after_xor);

    // P-Layer (not applied on last round)
    assign after_perm = pLayer(after_sbox);

    // Next key
    assign next_key   = keySchedule(key_reg, round_cnt + 5'd1);

    // -----------------------------------------------------------------------
    // Sequential logic
    // -----------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_reg  <= {BLOCK_WIDTH{1'b0}};
            key_reg    <= {KEY_WIDTH{1'b0}};
            round_cnt  <= 5'd0;
            running    <= 1'b0;
            ciphertext <= {BLOCK_WIDTH{1'b0}};
            done       <= 1'b0;
        end else begin
            done <= 1'b0; // default

            if (start) begin
                state_reg <= plaintext;
                key_reg   <= key;
                round_cnt <= 5'd0;
                running   <= 1'b1;
            end else if (running) begin
                if (round_cnt == 5'd30) begin
                    // Last round (round 31): AddRoundKey with K_31, S-Layer, no P-Layer
                    // Then final AddRoundKey with K_32
                    // Actually PRESENT has 31 rounds of (AddRoundKey + SBox + PLayer)
                    // then a final AddRoundKey with K_32
                    // Round 31 is the last full round (index 30 since we start at 0)
                    // After round 31, do final key addition
                    // We do: state = pLayer(sLayer(state XOR K_round))
                    // then final: state = state XOR K_32
                    // Let's handle round 31 normally (with perm), then final key add
                    // Actually per spec: rounds 1..31 each do AddRoundKey+SBox+PLayer
                    // then final AddRoundKey with K_32
                    // So at round_cnt==30 we do round 31, then final key add
                    begin
                        // Do round 31
                        state_reg <= after_perm;
                        key_reg   <= next_key; // this gives K_32
                        round_cnt <= round_cnt + 5'd1;
                        // next cycle will do final key add
                    end
                end else if (round_cnt == 5'd31) begin
                    // Final AddRoundKey with K_32
                    ciphertext <= state_reg ^ key_reg[79:16];
                    done       <= 1'b1;
                    running    <= 1'b0;
                    round_cnt  <= 5'd0;
                end else begin
                    // Rounds 1..30 (round_cnt 0..29)
                    state_reg <= after_perm;
                    key_reg   <= next_key;
                    round_cnt <= round_cnt + 5'd1;
                end
            end
        end
    end

endmodule
