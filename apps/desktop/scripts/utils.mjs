
import { pathToFileURL } from 'node:url';

// returns true if the passsed file is being invoked from node,
// not imported.
export function isMain(importMetaUrl) {
    const entryPath = process.argv[1];
    return typeof entryPath === 'string' && importMetaUrl === pathToFileURL(entryPath).href;
}
