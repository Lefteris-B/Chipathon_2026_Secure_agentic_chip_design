module present80 #(
    parameter int BLOCK_BITS = 64,
    parameter int KEY_BITS   = 80,
    parameter int LOAD_BITS  = 144,
    parameter int ROUNDS     = 31
)(
    input  logic clk,
    input  logic rst_n,
    input  logic load_en,
    input  logic din,
    input  logic shift_out_en,
    output logic dout,
    output logic done
);

    function automatic logic [3:0] sbox4(input logic [3:0] x);
        logic [3:0] tbl [16];
        begin
            tbl[0]=4'hC; tbl[1]=4'h5; tbl[2]=4'h6; tbl[3]=4'hB;
            tbl[4]=4'h9; tbl[5]=4'h0; tbl[6]=4'hA; tbl[7]=4'hD;
            tbl[8]=4'h3; tbl[9]=4'hE; tbl[10]=4'hF; tbl[11]=4'h8;
            tbl[12]=4'h4; tbl[13]=4'h7; tbl[14]=4'h1; tbl[15]=4'h2;
            sbox4 = tbl[x];
        end
    endfunction

    function automatic logic [63:0] player(input logic [63:0] s);
        logic [63:0] o;
        int p;
        begin
            o = 64'd0;
            for (int i = 0; i < 64; i++) begin
                p = (i == 63) ? 63 : ((i * 16) % 63);
                o[p] = s[i];
            end
            player = o;
        end
    endfunction

    logic [BLOCK_BITS-1:0] state;
    logic [KEY_BITS-1:0]   key;
    logic [7:0]            load_cnt;
    logic [5:0]            round_cnt;
    logic                  running;
    logic                  finalizing;
    logic                  done_r;

    logic [BLOCK_BITS-1:0] ark, sub, perm;

    always_comb begin
        ark = state ^ key[KEY_BITS-1 -: BLOCK_BITS];
        for (int i = 0; i < 16; i++) begin
            sub[i*4 +: 4] = sbox4(ark[i*4 +: 4]);
        end
        perm = player(sub);
    end

    function automatic logic [KEY_BITS-1:0] update_key(
        input logic [KEY_BITS-1:0] k,
        input logic [4:0] rc
    );
        logic [KEY_BITS-1:0] r;
        begin
            r = {k[18:0], k[79:19]};
            r[79:76] = sbox4(r[79:76]);
            r[19:15] = r[19:15] ^ rc;
            update_key = r;
        end
    endfunction

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= '0;
            key        <= '0;
            load_cnt   <= '0;
            round_cnt  <= 6'd0;
            running    <= 1'b0;
            finalizing <= 1'b0;
            done_r     <= 1'b0;
        end else begin
            if (load_en) begin
                if (load_cnt < 8'(LOAD_BITS)) begin
                    {key, state} <= {key[KEY_BITS-2:0], state, din};
                    load_cnt <= load_cnt + 8'd1;
                end
                round_cnt  <= 6'd0;
                running    <= 1'b0;
                finalizing <= 1'b0;
                done_r     <= 1'b0;
            end else if (load_cnt >= 8'(LOAD_BITS) && !done_r && !running && !finalizing) begin
                running   <= 1'b1;
                round_cnt <= 6'd1;
            end else if (running) begin
                // apply round: addRoundKey, sBox, pLayer, then key update
                state <= perm;
                key   <= update_key(key, round_cnt[4:0]);
                if (round_cnt == 6'(ROUNDS)) begin
                    running    <= 1'b0;
                    finalizing <= 1'b1;
                end else begin
                    round_cnt <= round_cnt + 6'd1;
                end
            end else if (finalizing) begin
                // final addRoundKey using post-round-31 key
                state      <= state ^ key[KEY_BITS-1 -: BLOCK_BITS];
                finalizing <= 1'b0;
                done_r     <= 1'b1;
            end else if (done_r) begin
                if (shift_out_en) begin
                    state <= {state[BLOCK_BITS-2:0], 1'b0};
                end
            end
        end
    end

    assign dout = (done_r && shift_out_en) ? state[BLOCK_BITS-1] : 1'b0;
    assign done = done_r;

endmodule
