// источник: Flutter 3.47.5, packages/flutter_tools/lib/src/android/gradle.dart, строки 60–73 и 1085–1125
const _kBuildVariantRegexGroupName = 'variant';
const _kBuildVariantTaskName = 'printBuildVariants';
const _kSdkManagerPathProperty = 'flutter.sdkManagerPath';
const _kAndroidSdkRootProperty = 'flutter.androidSdkRoot';
const _kInstalledNdkVersionsProperty = 'flutter.installedNdkVersions';
@visibleForTesting
const failedToStripDebugSymbolsErrorMessage = r'''
Release app bundle failed to strip debug symbols from native libraries.
Please run flutter doctor and ensure that the Android toolchain does not
report any issues.

Otherwise, file an issue at https://github.com/flutter/flutter/issues.''';

typedef _OutputParser = void Function(String line);
  required Logger logger,
  required FileSystem fileSystem,
  String? buildNumber,
}) {
  assert(buildModes.isNotEmpty);
  buildNumber ??= '1.0';

  logger.printStatus('\nConsuming the Module', emphasis: true);
  logger.printStatus('''
  1. Open ${fileSystem.path.join('<host>', 'app', 'build.gradle')}
  2. Ensure you have the repositories configured, otherwise add them:

      String storageUrl = System.env.$kFlutterStorageBaseUrl ?: "https://storage.googleapis.com"
      repositories {
        maven {
            url '${repoDirectory.path}'
        }
        maven {
            url "\$storageUrl/download.flutter.io"
        }
      }

  3. Make the host app depend on the Flutter module:

    dependencies {''');

  for (final buildMode in buildModes) {
    logger.printStatus("""
      ${buildMode}Implementation '$androidPackage:flutter_$buildMode:$buildNumber'""");
  }

  logger.printStatus('''
    }
''');

  if (buildModes.contains('profile')) {
    logger.printStatus('''

  4. Add the `profile` build type:

    android {
