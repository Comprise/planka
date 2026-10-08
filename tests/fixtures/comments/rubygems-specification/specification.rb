# источник: Ruby 3.4.10, lib/rubygems/specification.rb, строки 1950–1990 (heredoc <<-RUBY)
  end

  # :stopdoc:
  alias_method :has_test_suite?, :has_unit_tests?
  # :startdoc:

  def hash # :nodoc:
    name.hash ^ version.hash
  end

  def init_with(coder) # :nodoc:
    @installed_by_version ||= nil
    yaml_initialize coder.tag, coder.map
  end

  eval <<-RUBY, binding, __FILE__, __LINE__ + 1
    # frozen_string_literal: true

    def set_nil_attributes_to_nil
      #{@@nil_attributes.map {|key| "@#{key} = nil" }.join "; "}
    end
    private :set_nil_attributes_to_nil

    def set_not_nil_attributes_to_default_values
      #{@@non_nil_attributes.map {|key| "@#{key} = #{INITIALIZE_CODE_FOR_DEFAULTS[key]}" }.join ";"}
    end
    private :set_not_nil_attributes_to_default_values
  RUBY

  ##
  # Specification constructor. Assigns the default values to the attributes
  # and yields itself for further initialization.  Optionally takes +name+ and
  # +version+.

  def initialize(name = nil, version = nil)
    super()
    @gems_dir              = nil
    @base_dir              = nil
    @loaded = false
    @activated = false
    @loaded_from = nil
