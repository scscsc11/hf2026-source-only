export interface ValidationResult {
    valid: boolean;
    /** Human-readable error list (empty when valid). */
    errors: string[];
}
/** Validate an inbound sim:state frame against sim-state.schema.json. */
export declare function validateSimState(frame: unknown): ValidationResult;
/** Validate an outbound sim:commands payload against sim-commands.schema.json. */
export declare function validateSimCommand(command: unknown): ValidationResult;
//# sourceMappingURL=schema-validator.d.ts.map