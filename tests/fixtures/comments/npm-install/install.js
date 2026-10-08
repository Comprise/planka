// источник: npm 12.2.0, lib/commands/install.js, строки 53–104
  static usage = ['[<package-spec> ...]']

  static async completion (opts) {
    const { partialWord } = opts
    // install can complete to a folder with a package.json, or any package.
    // if it has a slash, then it's gotta be a folder
    // if it starts with https?://, then just give up, because it's a url
    if (/^https?:\/\//.test(partialWord)) {
      // do not complete to URLs
      return []
    }

    if (/\//.test(partialWord)) {
      // Complete fully to folder if there is exactly one match and it is a folder containing a package.json file.
      // If that is not the case we return 0 matches, which will trigger the default bash complete.
      const lastSlashIdx = partialWord.lastIndexOf('/')
      const partialName = partialWord.slice(lastSlashIdx + 1)
      const partialPath = partialWord.slice(0, lastSlashIdx) || '/'

      const isDirMatch = async sibling => {
        if (sibling.slice(0, partialName.length) !== partialName) {
          return false
        }

        try {
          const contents = await readdir(join(partialPath, sibling))
          const result = (contents.indexOf('package.json') !== -1)
          return result
        } catch {
          return false
        }
      }

      try {
        const siblings = await readdir(partialPath)
        const matches = []
        for (const sibling of siblings) {
          if (await isDirMatch(sibling)) {
            matches.push(sibling)
          }
        }
        if (matches.length === 1) {
          return [join(partialPath, matches[0])]
        }
        // no matches
        return []
      } catch {
        return [] // invalid dir: no matching
      }
    }
    // Note: there used to be registry completion here, but it stopped making sense somewhere around 50,000 packages on the registry
  }
