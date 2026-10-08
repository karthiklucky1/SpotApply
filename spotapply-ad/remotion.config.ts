/**
 * Note: When using the Node.JS APIs, the config file
 * doesn't apply. Instead, pass options directly to the APIs.
 *
 * All configuration options: https://remotion.dev/docs/config
 */

import { Config } from "@remotion/cli/config";

Config.setRspack(true);
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);

// Remotion downloads its own Chrome Headless Shell on first render. Where
// that download is blocked (sandboxed CI, offline machines), point it at an
// existing headless Chromium instead:
//   REMOTION_BROWSER_EXECUTABLE=/path/to/headless_shell npm run render
if (process.env.REMOTION_BROWSER_EXECUTABLE) {
  Config.setBrowserExecutable(process.env.REMOTION_BROWSER_EXECUTABLE);
}
