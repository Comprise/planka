# источник: ESP-IDF 6.0, components/bt/host/nimble/nimble/nimble/host/tools/log2smtest.rb, строки 469–487 (return <<-eos)
def pair_cmd_to_s(cmd, is_req)
    suffix = reqrsp_s(is_req)
    return <<-eos
        .pair_#{suffix} = {
            .io_cap = #{to_hex_s(cmd[:io_cap])},
            .oob_data_flag = #{to_hex_s(cmd[:oob_data_flag])},
            .authreq = #{to_hex_s(cmd[:authreq])},
            .max_enc_key_size = #{to_hex_s(cmd[:max_enc_key_size])},
            .init_key_dist = #{to_hex_s(cmd[:init_key_dist])},
            .resp_key_dist = #{to_hex_s(cmd[:resp_key_dist])},
        },
    eos
end

def privkey_to_s(privkey)
    return bytes_to_arr(privkey, "our_priv_key", 8)
end

def public_key_to_s(public_key, is_req)
