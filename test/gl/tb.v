// T1.3 gate-level vector test: same 17 transactions as runs/spi_slave_regs_r3/tb.
// Inputs change on negedge clk: the synchronizers see them as asynchronous inputs,
// and iverilog does not model setup/hold (STA signoff covers those).
`timescale 1ns/1ps
`ifndef HALF
 `define HALF 8
`endif
module vdd_conn(inout VDD); endmodule
module vss_conn(inout VSS); endmodule
module tb;
  reg clk=0, rst_n=0, din=0, load_en=0, shift_out_en=1;
  supply1 VDD; supply0 VSS;
  wire [25:0] t;
  wire dout_OUT, done_OUT;
`ifdef MOSI_STUCK
  wire din_w = 1'b0;
`else
  wire din_w = din;
`endif
  D10_D dut(.clk(clk),.rst_n(rst_n),.din(din_w),.load_en(load_en),.shift_out_en(shift_out_en),
    .dout_IN(1'b0),.done_IN(1'b0),.VDD(VDD),.VSS(VSS),.dout_OUT(dout_OUT),.done_OUT(done_OUT),
    .clk_PD(t[0]),.clk_PU(t[1]),.din_PD(t[2]),.din_PU(t[3]),.load_en_PD(t[4]),.load_en_PU(t[5]),
    .rst_n_PD(t[6]),.rst_n_PU(t[7]),.shift_out_en_PD(t[8]),.shift_out_en_PU(t[9]),
    .done_CS(t[10]),.done_IE(t[11]),.done_PD(t[12]),.done_PU(t[13]),.done_SL(t[14]),
    .dout_CS(t[15]),.dout_IE(t[16]),.dout_PD(t[17]),.dout_PU(t[18]),.dout_SL(t[19]),
    .done_OE(t[20]),.done_PDRV0(t[21]),.done_PDRV1(t[22]),.dout_OE(t[23]),.dout_PDRV0(t[24]),.dout_PDRV1(t[25]));
  always #6.5 clk=~clk; // 13 ns, the closed timing target
  integer errs=0, reads=0, dones=0, i, b;
  reg [15:0] word, rx;
  reg [31:0] prog [0:16];
  // done_OUT delay from the last clk rise, to prove the SDF took effect
  real tclk=0, ddly=-1;
  always @(posedge clk) tclk=$realtime;
  always @(posedge done_OUT) if (ddly<0) ddly=$realtime-tclk;
  task clks(input integer n); repeat(n) @(negedge clk); endtask
  task frame(input [7:0] cmd, input [7:0] data);
  begin
    word={cmd,data}; rx=0;
    shift_out_en=0; clks(`HALF);
    for (b=15;b>=0;b=b-1) begin
      din=word[b]; clks(`HALF);
      load_en=1; #1 rx={rx[14:0],dout_OUT};
      clks(`HALF); load_en=0;
    end
    clks(`HALF); shift_out_en=1; clks(`HALF);
  end endtask
  initial begin
`ifdef SDF
    $sdf_annotate(`SDF, dut);
`endif
    // {checked, rw, addr[6:0], data[7:0], expect[7:0]}
    prog[0]={7'd0,1'b1,1'b0,7'h00,8'h00,8'h5A}; prog[1]={7'd0,1'b1,1'b0,7'h01,8'h00,8'h01};
    prog[2]={7'd0,1'b1,1'b0,7'h02,8'h00,8'h00}; prog[3]={7'd0,1'b1,1'b0,7'h10,8'h00,8'h00};
    prog[4]={7'd0,1'b1,1'b0,7'h00,8'h00,8'h5A}; prog[5]={7'd0,1'b1,1'b0,7'h01,8'h00,8'h01};
    prog[6]={7'd0,1'b0,1'b1,7'h10,8'hA5,8'h00}; prog[7]={7'd0,1'b1,1'b0,7'h10,8'h00,8'hA5};
    prog[8]={7'd0,1'b0,1'b1,7'h13,8'h3C,8'h00}; prog[9]={7'd0,1'b1,1'b0,7'h13,8'h00,8'h3C};
    prog[10]={7'd0,1'b1,1'b0,7'h10,8'h00,8'hA5}; prog[11]={7'd0,1'b0,1'b1,7'h02,8'h77,8'h00};
    prog[12]={7'd0,1'b1,1'b0,7'h02,8'h00,8'h77}; prog[13]={7'd0,1'b0,1'b1,7'h11,8'h0F,8'h00};
    prog[14]={7'd0,1'b1,1'b0,7'h11,8'h00,8'h0F}; prog[15]={7'd0,1'b1,1'b0,7'h7E,8'h00,8'h00};
    prog[16]={7'd0,1'b1,1'b0,7'h13,8'h00,8'h3C};
`ifdef BAD_EXPECT
    prog[0][7:0]=8'h5B;
`endif
    clks(4); rst_n=1; clks(4);
    for (i=0;i<17;i=i+1) begin
      frame(prog[i][23:16], prog[i][15:8]);
      if (done_OUT===1'b1) dones=dones+1;
      else begin errs=errs+1; $display("FAIL txn=%0d done=%b",i,done_OUT); end
      if (prog[i][24]) begin
        reads=reads+1;
        if (rx[7:0]!==prog[i][7:0]) begin errs=errs+1; $display("FAIL txn=%0d miso exp=%h got=%h",i,prog[i][7:0],rx[7:0]); end
      end
    end
    if (t!==26'b11_1111_0000_0000_0000_0000_0000) begin errs=errs+1; $display("FAIL tie-offs t=%b",t); end
    $display("%s txns=17 reads=%0d done=%0d errs=%0d done_clk2q=%0.3fns",errs==0?"GLPASS":"GLFAIL",reads,dones,errs,ddly);
    $finish;
  end
  initial begin #20000000 $display("GLFAIL timeout"); $finish; end
endmodule
