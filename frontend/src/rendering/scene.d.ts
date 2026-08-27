import * as THREE from 'three';
export interface SceneConfig {
    container: HTMLElement;
    antialias?: boolean;
    backgroundColor?: number;
}
/**
 * Performance defaults for the 019 scene.
 *
 * These are exported as named constants + a pure helper so they can be
 * unit-tested without instantiating a WebGLRenderer (which can't run
 * under vitest's node environment). SceneManager reads them at construct
 * time so changing them here changes the live scene.
 */
export declare const PIXEL_RATIO_CAP = 1.5;
export declare const SHADOWS_ENABLED_BY_DEFAULT = false;
/** Clamp the device pixel ratio to PIXEL_RATIO_CAP. Pure + testable. */
export declare function capPixelRatio(devicePixelRatio: number): number;
export interface PerformanceReport {
    fps: number;
    frameTime: number;
}
export type PerformanceCallback = (report: PerformanceReport) => void;
export interface SelectableObject {
    object: THREE.Object3D;
    entityType: 'uav' | 'target' | 'gimbal';
    entityId?: string;
}
export type EntityClickCallback = (entityType: 'uav' | 'target' | 'gimbal', entityId?: string) => void;
export declare class SceneManager {
    private scene;
    private camera;
    private renderer;
    private controls;
    private animationId;
    private updateCallbacks;
    private perfCallback;
    private frameCount;
    private perfElapsed;
    private raycaster;
    private mouse;
    private selectables;
    private onClickCallback;
    private gridHelper;
    private followTarget;
    private isFollowing;
    private keysDown;
    private keyPanSpeed;
    constructor(config: SceneConfig);
    /**
     * Toggle real-time shadow mapping. Disabled by default because on the
     * 019 scene (10 UAV + 30 vehicle + terrain mesh) the per-frame
     * 1024x1024 PCF shadow pass over a 6000x6000 frustum was the single
     * biggest frame-time contributor, dropping the rate to 5-20 FPS.
     * Operators who want shadows for a still/slow scene can re-enable them.
     */
    setShadowsEnabled(enabled: boolean): void;
    private addLights;
    private addGroundGrid;
    /**
     * Show or hide the ground grid.
     */
    setGridVisible(visible: boolean): void;
    /**
     * Keyboard controls for camera translation. We track pressed keys in a
     * Set and poll each frame in the animation loop (better than
     * keydown-driven jumps — gives smooth continuous movement). Only
     * reacts when the canvas/container has focus OR no input element is
     * focused (so typing in a text field doesn't move the camera).
     */
    private setupKeyboardControls;
    /**
     * Apply one frame of keyboard-driven camera translation. Called from
     * the animation loop. Moves both the camera AND the orbit target so
     * the view pans as a whole (otherwise the camera would orbit while
     * translating). Movement is along the camera's horizontal basis
     * (forward/right projected onto the XZ plane) + Q/E for vertical.
     */
    private applyKeyboardPan;
    add(object: THREE.Object3D): void;
    remove(object: THREE.Object3D): void;
    registerSelectable(obj: SelectableObject): void;
    onEntityClick(callback: EntityClickCallback): void;
    follow(position: THREE.Vector3): void;
    unfollow(): void;
    moveCameraTo(position: THREE.Vector3): void;
    get isFollowingMode(): boolean;
    private onClick;
    onUpdate(callback: (delta: number) => void): void;
    onPerformanceReport(callback: PerformanceCallback): void;
    start(): void;
    stop(): void;
    private clock;
    private onWindowResize;
    getScene(): THREE.Scene;
    getCamera(): THREE.PerspectiveCamera;
    getRenderer(): THREE.WebGLRenderer;
}
//# sourceMappingURL=scene.d.ts.map