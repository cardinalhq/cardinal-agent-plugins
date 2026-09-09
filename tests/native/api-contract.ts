import type { Plugin } from "@opencode-ai/plugin";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import openCode from "../../dist/native/opencode/index.js";
import pi from "../../dist/native/pi/index.js";

// Compiled against the published upstream APIs, not locally invented hook types.
const openCodePlugin: Plugin = openCode;
const piExtension: (api: ExtensionAPI) => void = pi;
void openCodePlugin;
void piExtension;
