// источник: формы go.dev/ref/mod, «go.mod files» (go, toolchain, godebug, require с // indirect, exclude,
// replace на модуль, retract, tool одной строкой и блоком)
module example.com/my/thing

go 1.24.0

toolchain go1.24.2

godebug default=go1.21

godebug (
	panicnil=1
	asynctimerchan=0
)

require golang.org/x/net v1.2.3

require (
	// Генератор кода для tool ниже.
	golang.org/x/tools v0.31.0
	golang.org/x/crypto v1.4.5 // indirect
	golang.org/x/text v1.6.7 // текст
	github.com/golang/mock v1.6.0
	honnef.co/go/tools v0.6.1
)

exclude golang.org/x/net v1.2.3

replace golang.org/x/net v1.2.3 => example.com/fork/net v1.4.5

retract [v1.9.0, v1.9.5]

tool golang.org/x/tools/cmd/stringer

tool (
	github.com/golang/mock/mockgen
	honnef.co/go/tools/cmd/staticcheck
)
