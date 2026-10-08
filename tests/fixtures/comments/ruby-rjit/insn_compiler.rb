# источник: Ruby 3.4.10, lib/ruby_vm/rjit/insn_compiler.rb, строки 3430–3482 (символ :/)
      register_cfunc_method(Kernel, :block_given?, :jit_rb_f_block_given_p)

      # Thread.current
      register_cfunc_method(C.rb_singleton_class(Thread), :current, :jit_thread_s_current)

      #---
      register_cfunc_method(Array, :<<, :jit_rb_ary_push)
      register_cfunc_method(Integer, :*, :jit_rb_int_mul)
      register_cfunc_method(Integer, :/, :jit_rb_int_div)
      register_cfunc_method(Integer, :[], :jit_rb_int_aref)
      register_cfunc_method(String, :getbyte, :jit_rb_str_getbyte)
    end

    def register_cfunc_method(klass, mid_sym, func)
      mid = C.rb_intern(mid_sym.to_s)
      me = C.rb_method_entry_at(klass, mid)

      assert_equal(false, me.nil?)

      # Only cfuncs are supported
      method_serial = me.def.method_serial

      @cfunc_codegen_table[method_serial] = method(func)
    end

    def lookup_cfunc_codegen(cme_def)
      @cfunc_codegen_table[cme_def.method_serial]
    end

    def stack_swap(_jit, ctx, asm, offset0, offset1)
      stack0_mem = ctx.stack_opnd(offset0)
      stack1_mem = ctx.stack_opnd(offset1)

      mapping0 = ctx.get_opnd_mapping(StackOpnd[offset0])
      mapping1 = ctx.get_opnd_mapping(StackOpnd[offset1])

      asm.mov(:rax, stack0_mem)
      asm.mov(:rcx, stack1_mem)
      asm.mov(stack0_mem, :rcx)
      asm.mov(stack1_mem, :rax)

      ctx.set_opnd_mapping(StackOpnd[offset0], mapping1)
      ctx.set_opnd_mapping(StackOpnd[offset1], mapping0)
    end

    def jit_getlocal_generic(jit, ctx, asm, idx:, level:)
      # Load environment pointer EP (level 0) from CFP
      ep_reg = :rax
      jit_get_ep(asm, level, reg: ep_reg)

      # Load the local from the block
      # val = *(vm_get_ep(GET_EP(), level) - idx);
      asm.mov(:rax, [ep_reg, -idx * C.VALUE.size])
