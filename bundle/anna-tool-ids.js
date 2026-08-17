/* Handle → platform tool_id map.
 *
 * `anna-app apps publish` rewrites this file with the real, platform-assigned
 * tool_id once the bundled Executa is published. Do NOT hand-edit the ids.
 *
 * The fallback below is only used by `anna-app dev`, where no platform id
 * exists yet and the local harness registers the executa under its dev id.
 */
window.__ANNA_TOOL_IDS__ = window.__ANNA_TOOL_IDS__ || {
  "error-journal": "tool-dev-error-journal"
};
