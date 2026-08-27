import * as THREE from 'three';
export interface LabelOptions {
    /** Sprite text (e.g. "UAV", "TARGET", "DECOY"). */
    text: string;
    /** CSS colour for the rendered text. Defaults to 'white'. */
    fillStyle?: string;
}
/**
 * Build a 128x64 canvas sprite with a translucent black background and
 * centred bold text. Scale (10, 5, 1) matches every prior caller so the
 * on-screen size is unchanged.
 */
export declare function createLabel(opts: LabelOptions): THREE.Sprite;
//# sourceMappingURL=label-factory.d.ts.map