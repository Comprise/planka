// источник: Flutter 3.47.5, packages/flutter_tools/gradle/resolve_dependencies.gradle.kts, строки 1–48
import java.nio.file.Paths

// This script is used to warm the Gradle cache by downloading the Flutter dependencies
// used during the build. This script is invoked when `flutter precache` is run.
//
// Command:
//  gradle -b <flutter-sdk>packages/flutter_tools/gradle/resolve_dependencies.gradle.kts
//      resolveDependencies
//
// This way, Gradle can run with the `--offline` flag later on to eliminate any
// network request during the build process.
//
// This includes:
//   1. The embedding
//   2. libflutter.so

val storageUrl: String = System.getenv("FLUTTER_STORAGE_BASE_URL") ?: "https://storage.googleapis.com"

val flutterRoot = projectDir.parentFile?.parentFile?.parentFile
requireNotNull(flutterRoot) { "Flutter root directory not found!" }

require(flutterRoot.isDirectory) { "Flutter root is not a valid directory!" }

val engineVersion =
    Paths
        .get(
            flutterRoot.absolutePath,
            "bin",
            "cache",
            "engine.stamp"
        ).toFile()
        .readText()
        .trim()

var engineRealm =
    Paths
        .get(
            flutterRoot.absolutePath,
            "bin",
            "cache",
            "engine.realm"
        ).toFile()
        .readText()
        .trim()

if (engineRealm.isNotEmpty()) {
    engineRealm += "/"
}
