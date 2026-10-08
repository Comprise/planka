// источник: формы go.mod монорепозитория из «Go Modules Reference» (replace на каталог одной строкой и блоком, replace на версию)
module example.com/monorepo/api

go 1.22.0

require (
	example.com/monorepo/shared v0.0.0-00010101000000-000000000000
	github.com/go-chi/chi/v5 v5.0.12
	google.golang.org/grpc v1.63.2
)

require (
	golang.org/x/net v0.22.0 // indirect
	golang.org/x/sys v0.18.0 // indirect
)

replace example.com/monorepo/shared => ../shared

replace (
	example.com/monorepo/proto v1.0.0 => ./proto
	google.golang.org/grpc => google.golang.org/grpc v1.62.0
)
