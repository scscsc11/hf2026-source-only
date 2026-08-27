import type { IncomingMessage, ServerResponse } from 'http';
import type { SimProcessManager } from './sim-process-manager';
export interface SimControlDeps {
    manager: SimProcessManager;
    scenariosDir: string;
    pythonBin: string;
    renderCtlBinary?: string;
    renderersDir?: string;
    /** 参赛者算法根目录(competition/user_algorithms); 缺省则不扫描参赛者算法。 */
    userAlgorithmsDir?: string;
    /** service 模式想定下发:SET sim:scenario 时改写 simulation.redis_host 的地址。 */
    advertiseRedisHost?: string;
}
/** 构造 HTTP 请求处理函数(注入 manager + 配置,便于测试)。 */
export declare function createSimControlHandler(deps: SimControlDeps): (req: IncomingMessage, res: ServerResponse) => Promise<void>;
//# sourceMappingURL=sim-control-endpoint.d.ts.map