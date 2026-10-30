export type ErrorCode =
  | "CONFIG_INVALID"
  | "ALIAS_NOT_FOUND"
  | "CHECK_NOT_FOUND"
  | "CHECK_SOURCE_REQUIRED"
  | "NAME_INVALID"
  | "SAME_LOCATION"
  | "SOURCE_MISSING"
  | "DESTINATION_EXISTS"
  | "DESTINATION_LOCKED"
  | "SENSITIVE_UNAVAILABLE"
  | "VALUE_LOCKED"
  | "VALUES_DIFFER"
  | "CONFIRMATION_REQUIRED"
  | "UNVERIFIED_DELETE_REQUIRES_ACK"
  | "PROVIDER_UNAVAILABLE"
  | "PROVIDER_ARGUMENTS"
  | "PROVIDER_AUTH"
  | "PROVIDER_INPUT"
  | "PROVIDER_PROMPT"
  | "PROVIDER_STDIN"
  | "PROVIDER_FAILED"
  | "PROVIDER_SCHEMA"
  | "LOCAL_FILE_UNSAFE"
  | "LOCAL_FILE_INVALID"
  | "STRUCTURED_FILE_INVALID"
  | "STDIN_REQUIRED"
  | "INPUT_TOO_LARGE"
  | "FANOUT_PARTIAL"
  | "ISSUER_EXISTS"
  | "ISSUER_OUTPUTS_REQUIRED"
  | "RULES_INSTALL_FAILED"
  | "TOOL_NOT_FOUND"
  | "TOOL_UNSAFE"
  | "TOOL_UNAVAILABLE"
  | "TOOL_TIMEOUT"
  | "VALUE_UNSUPPORTED"
  | "OUTPUT_TOO_LARGE"
  | "OUTPUT_UNSAFE";

const messages: Record<ErrorCode, string> = {
  CONFIG_INVALID: "Configuration is missing or invalid.",
  ALIAS_NOT_FOUND: "The requested alias is not configured.",
  CHECK_NOT_FOUND: "The requested audit check is not configured.",
  CHECK_SOURCE_REQUIRED: "The sync source must be a member of the requested audit check.",
  NAME_INVALID: "The environment-variable name is invalid.",
  SAME_LOCATION: "Source and destination resolve to the same name and location.",
  SOURCE_MISSING: "The requested name is not present at the source.",
  DESTINATION_EXISTS: "The destination already contains that name; use --overwrite to replace it.",
  DESTINATION_LOCKED: "The existing destination is locked and cannot be replaced safely.",
  SENSITIVE_UNAVAILABLE: "Vercel Sensitive storage is unavailable for the selected account or target.",
  VALUE_LOCKED: "The requested value is non-readable and cannot be used as a source.",
  VALUES_DIFFER: "The destination value could not be verified as equal.",
  CONFIRMATION_REQUIRED: "The source-deletion confirmation is missing or does not match the source name.",
  UNVERIFIED_DELETE_REQUIRES_ACK: "Deleting after an unverifiable Vercel Sensitive write requires explicit acknowledgement.",
  PROVIDER_UNAVAILABLE: "A required provider command is unavailable.",
  PROVIDER_ARGUMENTS: "The installed provider CLI rejected the generated command arguments.",
  PROVIDER_AUTH: "The provider rejected authorization for the requested operation.",
  PROVIDER_INPUT: "The provider rejected the private stdin value or non-interactive input mode.",
  PROVIDER_PROMPT: "The provider attempted an interactive prompt that is unsafe for agent use.",
  PROVIDER_STDIN: "The provider rejected secret input over stdin.",
  PROVIDER_FAILED: "The provider operation failed. Provider output was suppressed.",
  PROVIDER_SCHEMA: "The provider returned an unrecognized or unsafe response schema.",
  LOCAL_FILE_UNSAFE: "The local dotenv target is unsafe to access.",
  LOCAL_FILE_INVALID: "The local dotenv file is malformed or contains duplicate names.",
  STRUCTURED_FILE_INVALID: "The structured configuration file or allowlisted field is invalid.",
  STDIN_REQUIRED: "Secret input must be supplied through non-interactive stdin.",
  INPUT_TOO_LARGE: "Secret input exceeds the supported size.",
  FANOUT_PARTIAL: "Not every destination was updated; supported writes were rolled back.",
  ISSUER_EXISTS: "The provider already contains a credential with that configured name.",
  ISSUER_OUTPUTS_REQUIRED: "Every one-time credential output must have at least one destination.",
  RULES_INSTALL_FAILED: "Agent guardrails could not be installed safely.",
  TOOL_NOT_FOUND: "The requested tool is not declared under \"tools\" in the Keyper configuration.",
  TOOL_UNSAFE: "The tool command or its arguments could reveal secret values and were refused.",
  TOOL_UNAVAILABLE: "The tool command could not be started.",
  TOOL_TIMEOUT: "The tool exceeded its time limit and was stopped. Tool output was suppressed.",
  VALUE_UNSUPPORTED: "A configured value cannot be passed safely through a process environment.",
  OUTPUT_TOO_LARGE: "The tool output exceeded its size limit. Tool output was suppressed.",
  OUTPUT_UNSAFE: "The tool output is not UTF-8 text, so redaction cannot be guaranteed. Tool output was suppressed."
};

export class KeyperError extends Error {
  readonly code: ErrorCode;

  // Details carry only non-secret context such as configuration paths or rule names.
  constructor(code: ErrorCode, detail?: string) {
    super(detail ? `${messages[code]} ${detail}` : messages[code]);
    this.name = "KeyperError";
    this.code = code;
  }
}

export function asSafeError(error: unknown): KeyperError {
  return error instanceof KeyperError
    ? error
    : new KeyperError("PROVIDER_FAILED");
}
