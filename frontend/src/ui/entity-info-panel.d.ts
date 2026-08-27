import { SimulationState, ExtendedDetection } from '../core/wgs84-projection';
export type SelectedEntity = 'uav' | 'target' | 'gimbal' | null;
/** Determine if a detection block should be shown for this entity kind. */
export declare function shouldShowDetection(kind: string): boolean;
/** Format detection data into HTML table rows. Exported for testing. */
export declare function formatDetectionHtml(det: ExtendedDetection): string;
export declare class EntityInfoPanel {
    private container;
    /** Per-section collapse memory — survives #info-content innerHTML rebuilds. */
    private collapsedSections;
    /** Signature of the currently-rendered structure (uid + section set).
     *  When it is unchanged we update values in place instead of rebuilding the
     *  DOM — otherwise the per-frame rebuild tears down section headers mid-click
     *  and the toggle never fires (Spec 023). */
    private renderedSignature;
    constructor(containerId: string);
    private render;
    /**
     * Delegate the collapse toggle on the persistent container (not #info-content,
     * which is rebuilt on structural change) so the user's collapse state survives.
     */
    private bindSectionToggle;
    /** Apply the remembered collapse state to one section element. */
    private applySectionState;
    /** Apply remembered collapse states to all sections after a content rebuild. */
    private applyAllSectionStates;
    /** Structural signature: uid + which sections are present. Numerical values
     *  are intentionally excluded so live telemetry never triggers a rebuild. */
    private signature;
    /** Full rebuild HTML (only when structure changes). */
    private buildBodyHtml;
    /** In-place value update — keeps section header/body nodes alive so an
     *  in-flight click is not orphaned by a DOM rebuild. */
    private updateValues;
    /**
     * Spec 020 T6: selection is uid-driven (this.selectedEntity in App holds
     * the uid). This entry point is retained for the click-routing contract
     * but no longer stores a separate legacy slot.
     */
    select(_entity: SelectedEntity): void;
    /** Set the selected uid directly (from Entity Control or entity list). */
    selectByUid(_uid: string | null): void;
    update(state: SimulationState, selectedUid?: string | null): void;
    clear(): void;
}
//# sourceMappingURL=entity-info-panel.d.ts.map