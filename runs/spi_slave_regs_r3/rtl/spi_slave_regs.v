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
