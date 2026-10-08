// источник: Apache Groovy 4.0.32 (тег GROOVY_4_0_32), src/spec/test/SyntaxTest.groovy, строки 474–498 (многострочная slashy-строка, подстановка ${…})
    void testSlashyString() {
        // tag::slashy_1[]
        def fooPattern = /.*foo.*/
        assert fooPattern == '.*foo.*'
        // end::slashy_1[]

        // tag::slashy_2[]
        def escapeSlash = /The character \/ is a forward slash/
        assert escapeSlash == 'The character / is a forward slash'
        // end::slashy_2[]

        // tag::slashy_3[]
        def multilineSlashy = /one
            two
            three/

        assert multilineSlashy.contains('\n')
        // end::slashy_3[]

        // tag::slashy_4[]
        def color = 'blue'
        def interpolatedSlashy = /a ${color} car/

        assert interpolatedSlashy == 'a blue car'
        // end::slashy_4[]
