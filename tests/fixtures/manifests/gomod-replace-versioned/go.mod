// источник: формы go.dev/ref/mod, «replace directive» (замена с версией и без, на модуль и на каталог) и вложенного модуля с replace на корень `..`
module example.com/root/tools

go 1.22

require (
	example.com/root v0.0.0-00010101000000-000000000000
	golang.org/x/net v1.2.3
	golang.org/x/text v0.14.0
	golang.org/x/sync v0.7.0
)

replace example.com/root => ..

replace (
	golang.org/x/net v1.2.3 => ./fork/net
	golang.org/x/text v0.13.0 => ./fork/text
	golang.org/x/sync v0.7.0 => example.com/fork/sync v0.7.1
)
