import { EntityKind } from '../core/wgs84-projection';
export interface UidValidationResult {
    valid: boolean;
    kind: EntityKind | '';
}
/**
 * Validate that a unique_id exists in the current entities map.
 * Returns the entity kind when valid for type-based control rendering.
 */
export declare function isUidValid(uid: string | null | undefined, entities: Record<string, {
    kind: string;
}> | undefined | null): UidValidationResult;
//# sourceMappingURL=uid-validation.d.ts.map