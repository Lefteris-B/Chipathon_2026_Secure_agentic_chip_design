`timescale 1ns/1ps
// Self-checking testbench: drives the bit-serial present80 through the
// four standard PRESENT-80 test vectors and checks the ciphertext.
module tb_present80;

    reg  clk = 0;
    reg  rst_n, load_en, din, shift_out_en;
    wire dout, done;

    present80 dut (
        .clk(clk), .rst_n(rst_n), .load_en(load_en), .din(din),
        .shift_out_en(shift_out_en), .dout(dout), .done(done)
    );

    always #5 clk = ~clk;   // 10 ns period

    integer errors = 0;

    // Run one vector: load key+plaintext, wait for done, unload, compare.
    task run_vector;
        input [79:0] key;
        input [63:0] pt;
        input [63:0] expect_ct;
        integer i, guard;
        reg [63:0] ct;
        begin
            // reset between vectors
            rst_n = 0; load_en = 0; din = 0; shift_out_en = 0;
            @(negedge clk); @(negedge clk);
            rst_n = 1;

            // load 80 key bits MSB-first, then 64 plaintext bits MSB-first
            for (i = 79; i >= 0; i = i - 1) begin
                @(negedge clk); load_en = 1; din = key[i];
            end
            for (i = 63; i >= 0; i = i - 1) begin
                @(negedge clk); load_en = 1; din = pt[i];
            end
            @(negedge clk); load_en = 0; din = 0;   // 144th bit clocked at this posedge... deassert after

            // wait for done (bounded)
            guard = 0;
            while (!done && guard < 200) begin @(posedge clk); guard = guard + 1; end
            if (!done) begin
                $display("  FAIL: done never asserted"); errors = errors + 1; disable run_vector;
            end

            // unload 64 ciphertext bits MSB-first. Enable shift_out_en at a
            // negedge and sample the MSB before the shifting posedge, so the
            // first captured bit is state[63] (no premature shift).
            ct = 64'b0;
            for (i = 0; i < 64; i = i + 1) begin
                @(negedge clk);
                shift_out_en = 1;
                #1 ct[63 - i] = dout;   // let dout settle, sample MSB pre-shift
                @(posedge clk);         // shift takes effect here
            end
            shift_out_en = 0;

            if (ct === expect_ct)
                $display("  PASS  key=%020h pt=%016h ct=%016h", key, pt, ct);
            else begin
                $display("  FAIL  key=%020h pt=%016h", key, pt);
                $display("        got ct=%016h expected=%016h", ct, expect_ct);
                errors = errors + 1;
            end
        end
    endtask

    initial begin
        $display("PRESENT-80 vector check:");
        run_vector(80'h0000000000000000_0000, 64'h0000000000000000, 64'h5579C1387B228445);
        run_vector(80'hFFFFFFFFFFFFFFFF_FFFF, 64'h0000000000000000, 64'hE72C46C0F5945049);
        run_vector(80'h0000000000000000_0000, 64'hFFFFFFFFFFFFFFFF, 64'hA112FFC72F68417B);
        run_vector(80'hFFFFFFFFFFFFFFFF_FFFF, 64'hFFFFFFFFFFFFFFFF, 64'h3333DCD3213210D2);
        if (errors == 0) $display("ALL VECTORS PASS");
        else             $display("%0d VECTOR(S) FAILED", errors);
        $finish;
    end

endmodule
