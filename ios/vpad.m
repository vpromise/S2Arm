// 触屏虚拟手柄（基础版，设计稿参考分支 touch/design 的 docs/原生触摸方案设计.md §6.1）：完整 Switch 按键 + 左右摇杆，掌机模式下经 InputPump.InputHook 喂给 Ryujinx HID。
//   左半屏空白处按下 = 浮动左摇杆（按下点为中心，拖 60 pt 推满，松手消失）；右半屏空白处按下 = 浮动右摇杆（同上，转镜头）；
//   摇杆按下 LS / RS 三种按法：看得见的 LS / RS 键（默认布局：LS 在十字键上方、RS 在 ABXY 左下，照 Switch 手柄原位）；
//   浮动摇杆点一下不拖（≤0.25 秒、≤10 点）= 按一下；双击左 / 右半屏（第二下按住）= 按住。ABXY 右下按 Switch 原位（A 右、B 下、X 上、Y 左）；十字键左上；ZL L / R ZR 上缘两侧；－ ＋ 顶部中间。
//   ZL / ZR 按住 2 秒锁定（松手后游戏仍收到按下，键上小锁变闭锁 + 轻震），再点一下抬手解锁；功能开着时小锁常驻，按游戏在设置里可关。
//   碰屏恢复按键高亮，3 秒无触摸降到设定的最低透明度（缺省 25%，可调 0–100%）；热区不变。
// 线程：UIKit 主线程写，InputPump 的 HID 线程约 60 Hz 读 s2a_vpad_read（固定快照，不访问设备）。
// 触屏直通：游戏自己带一套 Switch 触屏 UI（main.bin 有 SwitchTouchInput / TouchInterface、"New touch event" 等符号，
//   开机就调 ActivateTouchScreen），但 S2Arm 一直只喂手柄，Hid.Touchscreen 里永远是 0 个触点，所以点屏幕没有任何反应。
//   「触屏布局…」列表里的两个内置项二选一：默认布局 / 自定义方案 = 只显示虚拟按键，触摸全被这层吃掉，游戏收不到任何触点；
//   「触屏直通」= 一个按键都不画、摇杆也不响应，所有手指直接进游戏，靠四指长按 1.5 s 的菜单切回来。
//   两套方案互斥：按键方案不发触点（否则按 A / 推摇杆都会被游戏当成点按，误触），直通方案不发手柄。
// 布局格式、文件管理与方案切换：vpad_layout / vpad_settings；拖拽编辑与吸附另行设计。
// 弱链接[ios,lab,touchtest]：s2a_ap_snapshot（自动玩家（C#）提供；链接行按这行放行未定义，runtime/build.mk 自动收）
// 弱链接[lab]：nxgal_present_frame（渲染层提供；输入工作台不链渲染层，ios/lab/build.sh 生成工程时注入）
// 弱链接[lab]：s2a_widen_game（runtime/src/widen.c：游戏自己出宽图 = 第二层；输入工作台不编宽屏模块，取不到按游戏不知道宽屏处理）
// 弱链接[lab]：s2a_game_canvas（runtime/src/widen.c：游戏画布 = 触点坐标空间，审计 P1-92；输入工作台取不到按 1280×720）
// 弱链接[lab]：s2a_screen_w_after_load（runtime/src/widen.c：装载后的屏幕宽，装载前回 0；直连还没挂出图层时上屏比例按它 : 1080。输入工作台取不到按拉伸铺满）
// 弱链接[lab]：s2a_direct_out_aspect（graphics/nvn/host_adapt.m：直连出图层当前的出图宽高比，触点矩形与上屏同一口径；输入工作台不链渲染层）
// 弱链接[lab]：s2a_dprompt_notice（dprompt.m 提供的提示条；输入工作台不编 dprompt.m，取不到就不提示）
// 弱链接[lab]：s2a_dprompt_ask（dprompt.m 提供的带两个按钮的提示条；输入工作台不编 dprompt.m，取不到就退回 s2a_dprompt_notice 或不提示）
#import <UIKit/UIKit.h>
#import <QuartzCore/QuartzCore.h>
#import "controller.h"
#import "vpad.h"
#import "vpad_settings.h"
#import "settings.h"
#include "s2a_docs.h"   // iOS 进程级容器 Documents（s2a_container_docs）
#include "s2a_pad.h"
#include "s2a_direct_prompt.h"   // 「键 / 触」切换钮与触屏提示的文案（与 launchers/shared/experience.v1.json prompts 逐字一致）
#include <stdatomic.h>
#include <pthread.h>
#include <sys/stat.h>
#include "native.h"
#include "s2a_unattended.h"
uint64_t s2a_ap_frames(void);                                   // ap_host.c：上屏帧数
int s2a_autoplay_on(void) __attribute__((weak_import));         // tests/autoplay：自动玩家在跑时不录
void s2a_ap_snapshot(const char *path) __attribute__((weak_import));
// graphics/metal/gal_present.m：最近一帧的真实出图尺寸（游戏自己的渲染像素）：触点坐标空间就用它。
int nxgal_present_frame(int32_t *w, int32_t *h, float *ax, float *ay, int32_t *stretched) __attribute__((weak_import));
int s2a_widen_game(void) __attribute__((weak_import));
void s2a_game_canvas(int *w, int *h) __attribute__((weak_import));   // 同上（runtime/src/widen.c，审计 P1-92 触点画布）
int s2a_screen_w_after_load(void) __attribute__((weak_import));
double s2a_direct_out_aspect(void) __attribute__((weak_import));

// ---- 录手柄（10-21 维护者 拍板：录 维护者 实机玩的虚拟手柄按键，正式局 MARK 后回放）----
// 人玩时缺省就录：按键 / 摇杆状态每变一次写一行「上屏帧号 \t 按键位 \t 左 x \t 左 y \t 右 x \t 右 y \t 录制起毫秒」到 Documents/padrec/<款>_<时刻>.tsv；
// 游戏每次存档提交（C# IFileSystem.Commit → s2a_padrec_mark）写一行「帧号 \t 存档 \t 第 N 次 \t 毫秒」、截 <同名>.save<N>.png，
// 返回 <同名>.save<N> 给 C# 把当时的存档整份拷进这个目录（services Saves.Snapshot）。每个存档点各留一份：退出时的自动存档不再盖掉玩家要的那次，
// 选哪个存档点回放由 tools/padrec_cut.py 定（之后有 ≥60 秒输入的最后一个）。自动玩家在跑时不录
static pthread_mutex_t g_rmu = PTHREAD_MUTEX_INITIALIZER; static FILE *g_rec; static char g_rpath[1024]; static int g_roff;
static uint32_t g_rb = ~0u; static uint64_t g_rs, g_rflush, g_rt0; static int g_nsave; static char g_rsave[1100];
static uint64_t rec_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000; }
static int rec_open(void) {   // 持 g_rmu
    if (g_rec || g_roff) return g_rec != NULL;
    if (s2a_autoplay_on && s2a_autoplay_on()) { g_roff = 1; return 0; }
    NSString *bid = NSBundle.mainBundle.bundleIdentifier ?: @"s2arm"; NSString *g = [bid componentsSeparatedByString:@"."].lastObject;
#if TARGET_OS_IPHONE
    char dir[900]; s2a_container_docs(dir, sizeof dir); strlcat(dir, "/padrec", sizeof dir); mkdir(dir, 0755);
#else   // 触屏对照工具（native-test-touch_compat）在 macOS 上编本文件：容器 Documents 只在 iOS 有，这里按文档根
    char dir[900]; s2a_docs_root(dir, sizeof dir); strlcat(dir, "/padrec", sizeof dir); mkdir(dir, 0755);
#endif
    time_t now = time(NULL); struct tm tm; localtime_r(&now, &tm); char ts[32]; strftime(ts, sizeof ts, "%m%d_%H%M%S", &tm);
    snprintf(g_rpath, sizeof g_rpath, "%s/%s_%s.tsv", dir, g.UTF8String, ts);
    g_rec = fopen(g_rpath, "w"); if (!g_rec) { g_roff = 1; LOG("【红】录手柄：建 %s 失败", g_rpath); return 0; }
    g_rt0 = rec_ms();
    fprintf(g_rec, "# 构建号 %s\n", s2a_main_build_id());   // 主程序构建号（exefs/main 0x40 起 16 字节）：执行器按它定 saves/<款>_<构建号>/
    fprintf(g_rec, "# 录手柄 款 %s：帧号（上屏）\t按键位\t左x\t左y\t右x\t右y\t毫秒；「帧号\t存档\tN\t毫秒」= 游戏第 N 次存档提交（存档副本 <同名>.saveN/、截图 <同名>.saveN.png）\n", g.UTF8String); fflush(g_rec);
    LOG("录手柄：写 %s", g_rpath); return 1;
}
static void rec_note(uint32_t b, uint64_t s) {   // HID 线程，约 60 Hz
    if (b == g_rb && s == g_rs) return;
    pthread_mutex_lock(&g_rmu);
    if (rec_open()) {
        g_rb = b; g_rs = s; uint64_t f = s2a_ap_frames();
        fprintf(g_rec, "%llu\t%#x\t%d\t%d\t%d\t%d\t%llu\n", (unsigned long long)f, b, (int16_t)(uint16_t)s, (int16_t)(uint16_t)(s >> 16), (int16_t)(uint16_t)(s >> 32), (int16_t)(uint16_t)(s >> 48), (unsigned long long)(rec_ms() - g_rt0));
        if (f >= g_rflush + 30) { fflush(g_rec); g_rflush = f; }
    }
    pthread_mutex_unlock(&g_rmu);
}
// C# 存档提交时调（services InputPump 按符号查，Mac 宿主没有这个导出）：返回本存档点的副本目录 <同名>.save<N>（C# 拷存档进去），不录时返回 NULL。
//   返回的缓冲在下一次调用前有效（调用方是 FS 服务线程，Commit 串行）
__attribute__((visibility("default"), used)) const char *s2a_padrec_mark(void) {
    if (s2a_pad_has_controller()) return NULL; // 实体手柄不触发触屏录制的截图、存档副本或文本写入。
    const char *ret = NULL;
    pthread_mutex_lock(&g_rmu);
    if (rec_open()) {
        uint64_t f = s2a_ap_frames(); int n = ++g_nsave;
        fprintf(g_rec, "%llu\t存档\t%d\t%llu\n", (unsigned long long)f, n, (unsigned long long)(rec_ms() - g_rt0)); fflush(g_rec);
        snprintf(g_rsave, sizeof g_rsave, "%.*s.save%d", (int)strlen(g_rpath) - 4, g_rpath, n);
        char png[1200]; snprintf(png, sizeof png, "%s.png", g_rsave);
        if (s2a_ap_snapshot) s2a_ap_snapshot(png);
        LOG("录手柄：第 %d 次存档提交（帧 %llu），截图 %s", n, (unsigned long long)f, png); ret = g_rsave;
    }
    pthread_mutex_unlock(&g_rmu);
    return ret;
}

// 按键位 = HidNpadButton 位序（Ryujinx ControllerKeys 同序）
enum { B_A = 0, B_B, B_X, B_Y, B_LS, B_RS, B_L, B_R, B_ZL, B_ZR, B_PLUS, B_MINUS, B_LEFT, B_UP, B_RIGHT, B_DOWN };
static _Atomic int g_reads;

// ---- 触点坐标空间（10-25 定案：直接取渲染层真值，不猜、不标定）----
// 触点要落在游戏自己的像素空间里。这个空间就是它这一帧真正出图的那块画布（cw×ch）：
//   Ryujinx 把 TouchState.X/Y 原样写进共享内存（compat/…/TouchDevice.cs:37，patches 无触摸补丁），
//   之后「触点 → 屏幕位置」是游戏内部做的，它归一化用的分母就是自己的渲染尺寸。
//   所以唯一不会错的做法是问渲染层：这一帧源贴图多大（nxgal_present_frame），触点空间就是多大。
// 为什么不再用「显示宽」（10-25 前一版）：那是从 S2ADisplay 报的分辨率推的，只在「游戏真按它出图」时成立；
//   宽屏点只标了 9 个、游戏可能照旧出 1280 的图再被拉伸铺满，这时分母是 1280，按 1564 发就整体偏右 22%。
//   实机两轮都留着几 % 到 20% 的残差，根子就在这层猜测上。
// 换设备为什么不用改：这条链路里一个设备相关的常数都没有——画面矩形来自上屏层这一帧的真实几何（gal_present.m 的
//   dst_rect 同一套算式），分母来自渲染层这一帧的真实出图尺寸，两者都是运行期量。所以 16:9（1920×1080、1334×750）、
//   19.5:9（2346×1080）、21:9、iPad 4:3（上下留边）走的是同一段代码：屏幕比 16:9 窄时上下带黑边，触点只落在画面内。
// 拿不到渲染层（直连模式 / 还没出第一帧）才退回显示宽，与旧行为一致。
static int g_src_w, g_src_h;   // 最近一帧真实出图尺寸（gameRect 里顺手填）
// 出图贴图是「游戏自己的尺寸 × 渲染倍率」（设置里的「渲染分辨率倍率」，S2A_RES_SCALE：0.5 / 0.75 / 1 / 1.5 / 2）。
//   倍率只改宿主侧的纹理像素，游戏看到的画布没变，所以触点空间要除回来——否则选 2 倍时触点会整体放大一倍。
//   倍率是起局前由 ios/settings.m 从 App 设置写进环境变量的（显式环境变量优先），与 GraphicsSettings 同一来源。
//   每次现读（settings.m s2a_ios_res_scale）：设置页「立刻重启」重写环境变量后进程内重启，不能沿用上一局的倍率（审计 P1-9）
static float g_res_scale(void) {
    static float last = 1.0f;
    float v = s2a_ios_res_scale(NSUserDefaults.standardUserDefaults);
    if (v != last) { last = v; LOG("触屏：渲染倍率 %gx，触点空间按游戏尺寸（出图 ÷ %g）算", v, v); }
    return v;
}
// 屏幕上打一行触屏真值（四指菜单里开关，缺省关）：手指点 → 触点 → 画面矩形 → 出图尺寸，一张截图就能定案。
static int g_hud_on;
static NSString *const kHudKey = @"S2A.touchHud";

static void note_read(s2a_pad_source source, uint32_t b, const int32_t *sticks) {
    uint64_t s = 0; for (int i = 0; i < 4; i++) s |= (uint64_t)(uint16_t)sticks[i] << (16 * i);
    if (atomic_fetch_add(&g_reads, 1) == 0) LOG("输入：HID 线程开始读取触屏 / 实体手柄快照");
    if (source == S2A_PAD_TOUCH) rec_note(b, s);
}
// C# InputPump 查这个导出接到 InputHook：返回按键位，sticks[4] = 左 x / y、右 x / y（±32767，上为正）
__attribute__((visibility("default"), used)) uint32_t s2a_vpad_read(int32_t *sticks) {
    s2a_pad_source source; uint32_t b = s2a_pad_read(sticks, &source);
    note_read(source, b, sticks);
    return b;
}
__attribute__((visibility("default"), used)) void s2a_vpad_read_ex(s2a_pad_snapshot *out) {
    s2a_pad_read_ex(out);
    note_read((s2a_pad_source)out->source, out->buttons, out->axes);
}

@interface S2AVPadView : UIView
- (void)setInputEnabled:(BOOL)enabled;
- (void)addLayoutAction:(UIAlertController *)menu controller:(UIViewController *)controller;
- (void)refreshTouchHud;
- (int)readTouches:(int32_t *)xy attributes:(uint32_t *)attributes;   // HID 线程调用，内部加锁
- (void)offerTouchHint;
- (int)setTouchOnly:(BOOL)on manual:(BOOL)manual why:(NSString *)why;
- (BOOL)touchOnly;
- (void)refreshMode;
@end
static __weak S2AVPadView *g_vpad;
static _Atomic int g_touch_only = -1;   // 当前方案（reloadLayout 写）：1 仅触屏、0 按键、-1 没挂
static _Atomic int g_user_chose = -1;   // 玩家手动选过方案：1 是、0 否、-1 没挂
// 触屏提示（审计 P1-3）提示过了；提示条由 dprompt.m 提供
static NSString *const kTouchHintKey = @"S2AVPadTouchHintShown";
void s2a_dprompt_notice(NSString *title, NSString *body, double delay, double seconds) __attribute__((weak_import));
void s2a_dprompt_ask(NSString *title, NSString *body, NSString *accept, NSString *decline, double delay, double seconds, void (^cb)(BOOL)) __attribute__((weak_import));
@implementation S2AVPadView {
    NSArray<UILabel *> *_keys;                         // 可见按键（tag = 位号）
    NSMapTable<UITouch *, NSNumber *> *_held;          // 触点 → 位号（按键）/ -1 左摇杆 / -2 右摇杆
    CGPoint _c[2]; UITouch *_stick[2]; UIView *_ring[2], *_knob[2];
    int16_t _ax[4]; uint32_t _stickPress;              // 摇杆量；双击按下的 LS / RS 位
    NSTimeInterval _lastUp[2]; CGPoint _lastUpAt[2];
    NSTimeInterval _downAt[2]; CGFloat _maxMove[2];    // 摇杆这一按的按下时刻、离按下点最远距离（点按 = 摇杆按下，S2AVPadStickTapIsPress）
    NSTimer *_fade;
    UIImpactFeedbackGenerator *_impactFeedback;
    S2AVPadHapticState _hapticState;
    S2AVPadHoldLockState _holdLock; NSTimer *_holdLockTimer; double _holdLockDue;   // ZL / ZR 按住锁定
    CGFloat _stickTravel[2];
    S2AVPadLayoutStore *_layoutStore;
    S2AVPadLayout *_layout;
    CGRect _safeRect, _lastBounds;
    CGFloat _radius[2]; CGRect _activation[2];
    NSString *_layoutProblem;
    BOOL _inputEnabled, _settingsOpen, _usingDefault;
    // 纯触屏模式（内置方案 touch）：虚拟按键一个不画，摇杆不响应，所有手指按原样写进 Hid.Touchscreen。
    //   槽位制：一根手指固定占一个下标，Start 只发首帧、End 发末帧，中间帧下标不变 = 游戏认得出是同一根手指。
    BOOL _touchOnly;
    int32_t _slotX[16], _slotY[16]; uint8_t _slotState[16]; BOOL _slotStart[16];   // 0 空闲 / 1 按住 / 2 等发 End
    NSMapTable<UITouch *, NSNumber *> *_slot;
    UILabel *_hud;                          // 触屏真值那一行（见 g_hud_on）
    UIButton *_gear; NSTimer *_gearFade;    // 游戏内设置入口：屏幕左上角常驻，闲置时淡到看得见但不抢画面
    UISegmentedControl *_mode;              // 「键 / 触」切换钮：游戏启用触屏后常驻屏幕上方正中（两种方案下都显示），一点就切
    BOOL _touchWanted;                      // 游戏启用过触屏（s2a_vpad_touch_wanted）
    int _autoTouch;                         // 自动切（游戏声明触屏配置，manual=0）的本次运行覆盖：1 = 本次按仅触屏跑、不改持久化选择；0 = 没有覆盖
    // 拖动编辑按键位置（11-07 维护者）：编辑期间不向游戏发输入，手指拖哪个键挪哪个键；完成存成本款游戏的「我的布局」并选中
    BOOL _editing;
    UIView *_editBar;
    NSMapTable<UITouch *, UILabel *> *_drag;
    NSMapTable<UITouch *, NSValue *> *_dragOffset;
}
static const CGFloat kHit = 10;
static const uint32_t kHoldLockKeys = (1u << B_ZL) | (1u << B_ZR);
// 方案三「描线」（11-07 维护者 选定）：无底色只描线 + 细投影，ABXY 字淡青，按下淡白填充；不扎眼、亮暗场景都看得清
static UIColor *vp_line(void) { return [UIColor colorWithWhite:1 alpha:0.78]; }
static UIColor *vp_text(void) { return [UIColor colorWithWhite:1 alpha:0.86]; }
static UIColor *vp_face(void) { return [UIColor colorWithRed:150 / 255.0 green:215 / 255.0 blue:230 / 255.0 alpha:0.9]; }
// 按下高亮与按键同形（11-07 维护者：圆钮按下出方框）：按键是 UILabel，UILabel 的 backgroundColor 画进自身内容、不受 cornerRadius 约束，
//   而投影要画在框外不能开 masksToBounds → 填充色只设到 layer.backgroundColor（图层背景按 cornerRadius 自带圆角）
static void vp_fill(UIView *key, BOOL pressed) { key.layer.backgroundColor = pressed ? [UIColor colorWithWhite:1 alpha:0.3].CGColor : NULL; }
// 全部虚拟按键 / 摇杆的整体透明度（11-07 维护者：游戏库设置里一个全局拉杆，全部游戏共用）：App 级偏好 S2AVPadOpacity 0–1，缺省 1；
//   乘在闲置 / 碰屏两档透明度之上（S2AVPadKeyOpacity）。进游戏时读一次（设置在游戏库里改，下次进游戏生效）
static CGFloat vp_opacity(void) {
    static CGFloat v = -1;
    if (v < 0) { id o = [NSUserDefaults.standardUserDefaults objectForKey:@"S2AVPadOpacity"];
        v = [o isKindOfClass:NSNumber.class] ? MAX(0.1, MIN(1, [o doubleValue])) : 1; }
    return v;
}
static void vp_shadow(CALayer *l) { l.shadowColor = UIColor.blackColor.CGColor; l.shadowOpacity = 0.45; l.shadowRadius = 1; l.shadowOffset = CGSizeMake(0, 1); }
// ZL / ZR 右侧的小锁：功能开着时常驻，开锁 = 没锁定，闭锁（淡青）= 锁定中。两张图建键时备好，切换只翻 highlighted。
static const NSInteger kLockBadgeTag = 1000;
static void vp_add_lock_badge(UIView *key) {
    UIImageSymbolConfiguration *size = [UIImageSymbolConfiguration configurationWithPointSize:10 weight:UIImageSymbolWeightBold];
    UIImageView *badge = [[UIImageView alloc] initWithImage:[[UIImage systemImageNamed:@"lock.open.fill" withConfiguration:size] imageWithTintColor:vp_text() renderingMode:UIImageRenderingModeAlwaysOriginal]
                                           highlightedImage:[[UIImage systemImageNamed:@"lock.fill" withConfiguration:size] imageWithTintColor:vp_face() renderingMode:UIImageRenderingModeAlwaysOriginal]];
    badge.tag = kLockBadgeTag; badge.accessibilityIdentifier = @"vpad.holdLock.badge"; badge.translatesAutoresizingMaskIntoConstraints = NO; vp_shadow(badge.layer);
    [key addSubview:badge];
    [NSLayoutConstraint activateConstraints:@[[badge.trailingAnchor constraintEqualToAnchor:key.trailingAnchor constant:-6], [badge.centerYAnchor constraintEqualToAnchor:key.centerYAnchor]]];
}
static void vp_lock(UIView *key, BOOL enabled, BOOL locked) {
    UIImageView *badge = (UIImageView *)[key viewWithTag:kLockBadgeTag]; if (!badge) return;
    badge.hidden = !enabled; badge.highlighted = locked;
    key.accessibilityValue = !enabled ? nil : locked ? @"已锁定" : @"未锁定";
}
static NSArray<NSString *> *button_ids(void) {
    return @[@"A", @"B", @"X", @"Y", @"LS", @"RS", @"L", @"R", @"ZL", @"ZR", @"PLUS", @"MINUS", @"LEFT", @"UP", @"RIGHT", @"DOWN"];
}
static NSString *button_label(NSString *identifier) {
    NSDictionary *labels = @{@"PLUS": @"＋", @"MINUS": @"－", @"UP": @"▲", @"DOWN": @"▼", @"LEFT": @"◀︎", @"RIGHT": @"▶︎"};
    return labels[identifier] ?: identifier;
}
- (instancetype)initWithFrame:(CGRect)f {
    if (!(self = [super initWithFrame:f])) return nil;
    self.multipleTouchEnabled = YES; self.backgroundColor = UIColor.clearColor; self.layer.zPosition = 1000;   // 压在其它子视图之上（直连出图层 11-02 起挂在最底层，host_adapt.m）
    _held = [NSMapTable strongToStrongObjectsMapTable];
    _slot = [NSMapTable strongToStrongObjectsMapTable];
    _inputEnabled = YES;
    g_hud_on = [NSUserDefaults.standardUserDefaults boolForKey:kHudKey];
    NSURL *docs = [NSFileManager.defaultManager URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
    // 布局与选择按游戏存（11-07 维护者：按游戏单独记住、不套用到其他游戏）：多款容器壳选游戏后 S2A_DOCS = Documents/games/<游戏号>（launchctx.c），
    //   单款壳 = Documents，与原先同一目录；闲置透明度 / 触感 / 摇杆手感仍按 App 存（全部游戏共用）
    const char *gd = getenv("S2A_DOCS"); NSURL *gameDocs = gd && *gd ? [NSURL fileURLWithPath:@(gd) isDirectory:YES] : docs;
    _layoutStore = [[S2AVPadLayoutStore alloc] initWithDirectory:[gameDocs URLByAppendingPathComponent:@"VPadLayouts" isDirectory:YES]
                                                settingsDirectory:[docs URLByAppendingPathComponent:@"VPadLayouts" isDirectory:YES]];
    [self reloadLayout];
    _mode = [[UISegmentedControl alloc] initWithItems:@[@S2A_DPROMPT_VPAD_SEG_KEYS, @S2A_DPROMPT_VPAD_SEG_TOUCH]];
    _mode.accessibilityIdentifier = @"vpad.mode"; _mode.hidden = YES; _mode.alpha = 0.7;
    _mode.backgroundColor = [UIColor colorWithWhite:0 alpha:0.35]; _mode.selectedSegmentTintColor = [UIColor colorWithWhite:1 alpha:0.85];
    [_mode setTitleTextAttributes:@{NSForegroundColorAttributeName: [UIColor colorWithWhite:1 alpha:0.9], NSFontAttributeName: [UIFont boldSystemFontOfSize:13]} forState:UIControlStateNormal];
    [_mode setTitleTextAttributes:@{NSForegroundColorAttributeName: UIColor.blackColor} forState:UIControlStateSelected];
    [_mode addTarget:self action:@selector(modeTapped:) forControlEvents:UIControlEventValueChanged];
    [self addSubview:_mode];
    _gear = [UIButton buttonWithType:UIButtonTypeSystem]; _gear.accessibilityIdentifier = @"vpad.settings"; _gear.accessibilityLabel = @"设置";
    [_gear setImage:[UIImage systemImageNamed:@"gearshape.fill"] forState:UIControlStateNormal]; _gear.tintColor = UIColor.whiteColor;
    _gear.backgroundColor = [UIColor colorWithWhite:0 alpha:0.45]; _gear.layer.cornerRadius = 18;
    [_gear addTarget:self action:@selector(gearTapped) forControlEvents:UIControlEventTouchUpInside];
    [self addSubview:_gear]; [self wakeGear];
    for (int i = 0; i < 2; i++) {
        _ring[i] = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 124, 124)]; _ring[i].layer.cornerRadius = 62;
        // 摇杆区用方案二「石墨」（11-07 维护者：B 方案的两个摇杆区替换 C 方案的）：深灰半透明实底、无描边，摇杆头浅灰
        _ring[i].layer.borderWidth = 0; _ring[i].backgroundColor = [UIColor colorWithRed:20 / 255.0 green:22 / 255.0 blue:26 / 255.0 alpha:0.32];
        _knob[i] = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 52, 52)]; _knob[i].layer.cornerRadius = 26;
        _knob[i].backgroundColor = [UIColor colorWithRed:210 / 255.0 green:214 / 255.0 blue:220 / 255.0 alpha:0.59];
        _ring[i].alpha = _knob[i].alpha = vp_opacity();
        _ring[i].hidden = _knob[i].hidden = YES; _ring[i].userInteractionEnabled = _knob[i].userInteractionEnabled = NO; [self addSubview:_ring[i]]; [self addSubview:_knob[i]];
    }
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(lostFocus:) name:UIApplicationWillResignActiveNotification object:nil];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(lostFocus:) name:UIApplicationDidEnterBackgroundNotification object:nil];
    [self wake];
    return self;
}
- (void)wakeGear {   // 刚显示时看得清，3 秒后淡到 0.35：玩家找得到，又不挡画面
    [_gearFade invalidate]; _gear.alpha = 0.9;
    _gearFade = [NSTimer scheduledTimerWithTimeInterval:3 repeats:NO block:^(NSTimer *t) { (void)t;
        [UIView animateWithDuration:0.4 animations:^{ self->_gear.alpha = 0.35; }]; }];
}
- (void)gearTapped { [self wakeGear]; void s2a_diag_open_menu(void); s2a_diag_open_menu(); }   // 同四指长按：游戏不退出，菜单开着时触屏输入停住
- (void)dealloc { [NSNotificationCenter.defaultCenter removeObserver:self]; [_gearFade invalidate]; [_fade invalidate]; [_holdLockTimer invalidate]; }
- (void)clearTouches {
    [_fade invalidate]; _fade = nil;
    [_held removeAllObjects]; _stickPress = 0;
    _hapticState = (S2AVPadHapticState){0}; memset(_stickTravel, 0, sizeof _stickTravel); _impactFeedback = nil;
    _holdLock = (S2AVPadHoldLockState){0}; [_holdLockTimer invalidate]; _holdLockTimer = nil; _holdLockDue = 0;   // 失焦 / 接手柄 / 开设置都放开：不留看不见的按住
    memset(_ax, 0, sizeof _ax); memset(_lastUp, 0, sizeof _lastUp);
    for (int i = 0; i < 2; i++) { _stick[i] = nil; _ring[i].hidden = _knob[i].hidden = YES; }
    [_slot removeAllObjects];
    @synchronized(self) {
        for (int i = 0; i < 16; i++) if (_slotState[i] == 1) _slotState[i] = 2;   // 按住的手指补一帧 End 再释放
    }
    for (UILabel *key in _keys) { vp_fill(key, NO); vp_lock(key, _layoutStore.holdLockEnabled, NO); }
    int32_t axes[4] = {0};
    // Disconnect clears pad.c's short-press latch as well as held state. Merely
    // publishing zero buttons leaves a press queued for the next HID read.
    s2a_pad_update(S2A_PAD_TOUCH, 0, 0, NULL);
    if (_inputEnabled && !_settingsOpen && !_editing) s2a_pad_update(S2A_PAD_TOUCH, 1, 0, axes);
}
- (void)lostFocus:(NSNotification *)notification { [self clearTouches]; }
- (CGRect)layoutSafeRect {
    UIEdgeInsets insets = self.safeAreaInsets;
    insets.left = MAX(insets.left, 16); insets.right = MAX(insets.right, 16); insets.bottom = MAX(insets.bottom, 8);
    CGRect rect = UIEdgeInsetsInsetRect(self.bounds, insets);
    return CGRectMake(rect.origin.x, rect.origin.y, MAX(1, rect.size.width), MAX(1, rect.size.height));
}
- (void)rebuildKeys {
    for (UILabel *key in _keys) [key removeFromSuperview];
    if (_touchOnly) { _keys = @[]; return; }   // 纯触屏：一个按键都不建，也就没有热点、没有长按能「呼出」的东西
    NSMutableArray *keys = [NSMutableArray array]; NSArray *ids = button_ids();
    for (NSDictionary *button in _layout.buttons) {
        UILabel *key = [UILabel new]; NSString *identifier = button[@"id"];
        key.text = button_label(identifier); key.tag = [ids indexOfObject:identifier]; key.textAlignment = NSTextAlignmentCenter;
        key.accessibilityIdentifier = [@"vpad.button." stringByAppendingString:identifier]; key.accessibilityLabel = identifier;
        BOOL face = key.tag >= B_A && key.tag <= B_Y;   // ABXY 字淡青
        key.textColor = face ? vp_face() : vp_text(); key.font = [UIFont boldSystemFontOfSize:(key.tag >= B_LEFT || key.tag == B_LS || key.tag == B_RS) ? 13 : face ? 17 : 14];
        key.backgroundColor = UIColor.clearColor; vp_fill(key, NO); key.layer.borderWidth = 1.6; key.layer.borderColor = vp_line().CGColor;
        key.layer.masksToBounds = NO; vp_shadow(key.layer);   // 投影要画在框外：不裁边
        if (kHoldLockKeys >> key.tag & 1) { vp_add_lock_badge(key); vp_lock(key, _layoutStore.holdLockEnabled, NO); }
        [self addSubview:key]; [keys addObject:key];
    }
    _keys = keys;
}
- (void)reloadLayout {
    [self clearTouches];
    NSError *error; S2AVPadLayout *selected = [_layoutStore selectedLayout:&error];
    _touchOnly = _autoTouch || [_layoutStore.selectedIdentifier isEqualToString:S2AVPadLayoutTouchIdentifier];   // 自动切只覆盖本次运行
    _usingDefault = !selected && !_touchOnly;
    _layout = selected ?: [S2AVPadLayout defaultForSafeRect:[self layoutSafeRect]];
    _layoutProblem = error.localizedDescription;
    if (error) LOG("触屏布局：选定方案加载失败，暂用默认布局：%s", error.localizedDescription.UTF8String);
    [self rebuildKeys]; [self setNeedsLayout]; [self wake];
    atomic_store(&g_touch_only, _touchOnly ? 1 : 0); atomic_store(&g_user_chose, _layoutStore.manualChosen ? 1 : 0); [self refreshMode];
    if (_touchOnly) { LOG("触屏布局：纯触屏（不显示虚拟按键，手指直接交给游戏）"); return; }
    LOG("触屏布局：应用 %s（schema 1）", _layout.identifier.UTF8String);
}
- (void)addLayoutAction:(UIAlertController *)menu controller:(UIViewController *)controller {
    if (!self.window || ![self isDescendantOfView:controller.view]) return;
    if (_layoutProblem.length) menu.message = @"选定的触屏布局未能加载，已临时使用默认布局。请打开触屏布局查看详情。";
    // 菜单是一次四指长按叫出来的：纯触屏模式下这四根手指已经进了游戏，收菜单时就地抬手，别在游戏里留下四点点按。
    [self clearTouches];
    __weak S2AVPadView *weakSelf = self;
    __weak UIAlertController *weakMenu = menu;
    [menu addAction:[UIAlertAction actionWithTitle:@"触屏布局…" style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        // The action sheet may still be presented while its handler executes.
        // Wait for its dismissal rather than stacking the settings navigation.
        void (^show)(void) = ^{ [weakSelf openLayouts]; };
        UIAlertController *sheet = weakMenu;
        if (sheet.presentingViewController) [sheet dismissViewControllerAnimated:YES completion:show];
        else dispatch_async(dispatch_get_main_queue(), show);
    }]];
    if (!_touchOnly) [menu addAction:[UIAlertAction actionWithTitle:@"编辑按键位置…" style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        void (^go)(void) = ^{ [weakSelf beginEditing]; };
        UIAlertController *sheet = weakMenu;
        if (sheet.presentingViewController) [sheet dismissViewControllerAnimated:YES completion:go];
        else dispatch_async(dispatch_get_main_queue(), go);
    }]];
    // 触屏真值一行：手指点 → 触点 → 画面矩形 → 出图尺寸。缺省关，开了才在屏幕下方显示（不拦触摸）。
    [menu addAction:[UIAlertAction actionWithTitle:(g_hud_on ? @"触屏诊断信息：开（点一下关）" : @"触屏诊断信息：关（点一下开）")
                                            style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        s2a_vpad_set_touch_hud(!g_hud_on);
    }]];
}
// ---------- 拖动编辑按键位置 ----------
- (UIButton *)editButton:(NSString *)title action:(SEL)action {
    UIButton *b = [UIButton buttonWithType:UIButtonTypeSystem]; [b setTitle:title forState:UIControlStateNormal];
    b.titleLabel.font = [UIFont boldSystemFontOfSize:15]; [b setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    b.backgroundColor = [UIColor colorWithWhite:0 alpha:0.55]; b.layer.cornerRadius = 16; b.layer.borderWidth = 1; b.layer.borderColor = vp_line().CGColor;
    [b addTarget:self action:action forControlEvents:UIControlEventTouchUpInside]; return b;
}
- (void)beginEditing {
    if (_editing || _touchOnly || _settingsOpen) return;
    _editing = YES; [self clearTouches]; [_fade invalidate]; _fade = nil;
    s2a_pad_update(S2A_PAD_TOUCH, 0, 0, NULL);   // 编辑期间触屏源不可用：游戏收不到半截按键
    _drag = [NSMapTable strongToStrongObjectsMapTable]; _dragOffset = [NSMapTable strongToStrongObjectsMapTable];
    for (UILabel *l in _keys) { [l.layer removeAllAnimations]; l.alpha = 1; l.layer.borderColor = vp_face().CGColor; }
    _editBar = [UIView new]; _editBar.accessibilityIdentifier = @"vpad.edit";
    UILabel *tip = [UILabel new]; tip.text = @"拖动按键调整位置（只对这款游戏生效）"; tip.textColor = UIColor.whiteColor; tip.font = [UIFont systemFontOfSize:13];
    tip.textAlignment = NSTextAlignmentCenter; tip.tag = 1;
    UIButton *reset = [self editButton:@"恢复默认" action:@selector(editReset)], *cancel = [self editButton:@"取消" action:@selector(editCancel)],
             *done = [self editButton:@"完成" action:@selector(editDone)];
    reset.tag = 2; cancel.tag = 3; done.tag = 4;
    for (UIView *v in @[tip, reset, cancel, done]) [_editBar addSubview:v];
    [self addSubview:_editBar]; [self setNeedsLayout];
    LOG("触屏布局：进入按键位置编辑（%s）", _layout.identifier.UTF8String);
}
- (void)endEditing {
    _editing = NO; [_editBar removeFromSuperview]; _editBar = nil; _drag = nil; _dragOffset = nil;
    for (UILabel *l in _keys) l.layer.borderColor = vp_line().CGColor;
}
- (void)editCancel { [self endEditing]; [self reloadLayout]; LOG("触屏布局：取消编辑，按键位置不变"); }
- (void)editReset {   // 恢复默认：只切回内置默认布局，本款「我的布局」文件保留（同管理页的恢复默认）
    NSError *error; [self endEditing];
    if (![_layoutStore selectIdentifier:S2AVPadLayoutDefaultIdentifier error:&error]) LOG("【红】触屏布局：恢复默认失败：%s", error.localizedDescription.UTF8String);
    else { [_layoutStore markManual:nil]; LOG("触屏布局：本款游戏恢复默认按键位置"); }
    [self reloadLayout];
}
- (void)editDone {
    NSMutableDictionary *centers = [NSMutableDictionary dictionary];
    for (NSUInteger i = 0; i < _keys.count && i < _layout.buttons.count; i++)
        centers[_layout.buttons[i][@"id"]] = @[@(_keys[i].center.x), @(_keys[i].center.y)];
    NSError *error;
    S2AVPadLayout *mine = [_layout layoutWithButtonCenters:centers safeRect:_safeRect identifier:S2AVPadLayoutMineIdentifier name:@"我的布局" error:&error];
    BOOL ok = mine && [_layoutStore replaceLayout:mine error:&error] && [_layoutStore selectIdentifier:S2AVPadLayoutMineIdentifier error:&error];
    [self endEditing];
    if (ok) { [_layoutStore markManual:nil]; LOG("触屏布局：本款游戏的按键位置已保存（我的布局）"); }
    else {
        LOG("【红】触屏布局：保存按键位置失败：%s", error.localizedDescription.UTF8String);
        UIViewController *c = self.window.rootViewController;
        if (c && !c.presentedViewController) {
            UIAlertController *a = [UIAlertController alertControllerWithTitle:@"按键位置没保存" message:error.localizedDescription preferredStyle:UIAlertControllerStyleAlert];
            [a addAction:[UIAlertAction actionWithTitle:@"好" style:UIAlertActionStyleDefault handler:nil]]; [c presentViewController:a animated:YES completion:nil];
        }
    }
    [self reloadLayout];
}
- (void)layoutEditBar {
    if (!_editBar) return;
    CGRect g = self.safeAreaLayoutGuide.layoutFrame; CGFloat w = MIN(520, g.size.width - 32), h = 76;
    _editBar.frame = CGRectMake(CGRectGetMidX(g) - w / 2, CGRectGetMidY(g) - h / 2, w, h); [self bringSubviewToFront:_editBar];   // 屏幕正中（默认布局正中没有键；放顶上会压住 − / +）
    [_editBar viewWithTag:1].frame = CGRectMake(0, 0, w, 24);
    CGFloat bw = (w - 2 * 12) / 3;
    for (int i = 0; i < 3; i++) [_editBar viewWithTag:2 + i].frame = CGRectMake(i * (bw + 12), 32, bw, 36);
}
- (void)openLayouts {
    UIViewController *controller = self.window.rootViewController;
    if (!controller || controller.presentedViewController || _settingsOpen) return;
    _settingsOpen = YES; [self clearTouches];
    __weak S2AVPadView *weakSelf = self;
    S2AVPadSettings *settings = [[S2AVPadSettings alloc] initWithStore:_layoutStore current:_layout changed:^{
        S2AVPadView *view = weakSelf; if (!view) return;
        [view->_layoutStore markManual:nil]; view->_autoTouch = 0;   // 设置里选方案 = 玩家手动选过，自动切的本次覆盖作废
        [view reloadLayout];
    } closed:^{
        S2AVPadView *view = weakSelf; if (!view) return;
        view->_settingsOpen = NO; [view clearTouches]; [view wake];
    }];
    UINavigationController *navigation = [[UINavigationController alloc] initWithRootViewController:settings];
    navigation.modalInPresentation = YES;
    [controller presentViewController:navigation animated:YES completion:nil];
}
- (void)setSettingsOpen:(BOOL)open {
    _settingsOpen = open; [self clearTouches]; if (!open) [self wake];
}
// 审计 P1-3：游戏启用了触屏（C# InputPump 读 Hid.Touchscreen.Active 后经 s2a_vpad_touch_wanted 转告）而当前是按键方案时，提示一次怎么切到「仅触屏操作」（本 App 只提示一次）。
//   两套方案保持互斥、不做「又有按键又透传」：默认布局左右半屏空白处都是浮动摇杆的激活区，按键以外没有空地可透传，透传就等于推摇杆时游戏也收到点按；
//   很多游戏开机就启用触屏、分不出真用不用，所以只提示、不自动切。自动化局不提示
//   11-04 维护者（脑锻炼标题页只认触屏，四指长按 1.5 秒进设置太深）：游戏启用触屏后屏幕上方常驻「键 / 触」切换钮（两种方案下都显示，自动化局不显示）；
//   提示条（本 App 只提示一次）带「切到触屏」按钮，一点就切。切换与持久化在 vpad_layout.m setTouchOnly:（单测 t_vpad_toggle）
- (void)offerTouchHint {
    _touchWanted = YES;
    if (s2a_unattended()) return;
    [self refreshMode]; _mode.hidden = NO; [self setNeedsLayout];
    LOG("触屏布局：游戏启用了触屏，显示「键 / 触」切换钮（当前 %s）", _touchOnly ? "触屏" : "按键");
    NSUserDefaults *u = NSUserDefaults.standardUserDefaults;
    if (_touchOnly || [u boolForKey:kTouchHintKey]) return;
    // 等 3 秒再定弹不弹：游戏常先启用触屏（ActivateTouchScreen）、再声明触屏配置（SetTouchScreenConfiguration → 一次成功线的自动切，
    //   ios/touch_config.m），脑锻炼就是这样。3 秒后已是仅触屏（被自动切了）就不弹「切到触屏」，也不记「提示过」，免得两条依次弹
    __weak S2AVPadView *weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        S2AVPadView *view = weakSelf; if (!view || view->_touchOnly) { if (view) LOG("触屏布局：3 秒内已切到仅触屏（自动切），不弹「切到触屏」"); return; }
        [u setBool:YES forKey:kTouchHintKey];
        if (s2a_dprompt_ask) s2a_dprompt_ask(@S2A_DPROMPT_VPAD_TOUCH_TITLE, @S2A_DPROMPT_VPAD_TOUCH_BODY, @S2A_DPROMPT_VPAD_TOUCH_ACCEPT, @S2A_DPROMPT_VPAD_TOUCH_DECLINE, 0, 15, ^(BOOL accept) {
            LOG("触屏布局：提示条 %s", accept ? "点了「切到触屏」" : "没切（先不用 / 超时）");
            if (accept) [weakSelf setTouchOnly:YES manual:YES why:@"提示条"];
        });
        else if (s2a_dprompt_notice) s2a_dprompt_notice(@S2A_DPROMPT_VPAD_TOUCH_TITLE, @S2A_DPROMPT_VPAD_TOUCH_BODY, 0, 15);
    });
}
- (BOOL)touchOnly { return _touchOnly; }
- (void)refreshMode { _mode.selectedSegmentIndex = _touchOnly ? 1 : 0; }
- (void)modeTapped:(UISegmentedControl *)control { [self setTouchOnly:control.selectedSegmentIndex == 1 manual:YES why:@"切换钮"]; }
// 切换方案（切换钮 / 提示条 / s2a_vpad_set_touch_only 共用；主线程）：返回 1 真切了、0 本来就是（或自动切遇到玩家手动选过）、-1 写盘失败（保持原方案）
//   manual：切换钮 / 提示条 / 设置 = 玩家手动，持久化选择 + 记「手动选过」、清掉自动切的本次覆盖；
//   自动（游戏声明触屏配置，一次成功线）：玩家手动选过就不切；否则只改本次运行（_autoTouch），不动持久化选择、不记手动
- (int)setTouchOnly:(BOOL)on manual:(BOOL)manual why:(NSString *)why {
    BOOL before = _touchOnly; int r;
    if (manual) {
        NSError *error; _autoTouch = 0; r = [_layoutStore setTouchOnly:on error:&error];
        if (r < 0) { LOG("【红】触屏布局：切到%s失败（%s）：%s", on ? "触屏" : "按键", why.UTF8String, error.localizedDescription.UTF8String); [self reloadLayout]; return -1; }
        [_layoutStore markManual:nil];
    } else {
        if (_layoutStore.manualChosen) { LOG("触屏布局：自动切到%s跳过（玩家手动选过方案；%s）", on ? "触屏" : "按键", why.UTF8String); return 0; }
        _autoTouch = on ? 1 : 0;
    }
    [self reloadLayout]; r = _touchOnly != before;
    if (r) LOG("触屏布局：切到%s（%s，%s）", _touchOnly ? "仅触屏操作" : "按键布局", why.UTF8String, manual ? "手动、已保存" : "自动、只本次运行");
    return r;
}
- (NSString *)layoutName {
    return _touchOnly ? @"仅触屏操作" : _usingDefault ? @"按键布局" : _layout.name;
}
- (UIViewController *)layoutController {
    __weak S2AVPadView *weakSelf = self;
    S2AVPadSettings *settings = [[S2AVPadSettings alloc] initWithStore:_layoutStore current:_layout changed:^{
        [weakSelf reloadLayout];
    } closed:nil];
    settings.embeddedInSettings = YES;
    settings.currentLayout = ^S2AVPadLayout *{ S2AVPadView *view = weakSelf; return view ? view->_layout : nil; };
    return settings;
}
- (void)layoutSubviews {
    [super layoutSubviews]; CGRect safe = [self layoutSafeRect];
    // 「键 / 触」切换钮：安全区上沿正中（默认布局四角是 L / ZL / R / ZR，正中上方没有按键）；提示条也在上方正中、压在它上面
    _mode.frame = CGRectMake(CGRectGetMidX(safe) - 44, CGRectGetMinY(self.safeAreaLayoutGuide.layoutFrame) + 4, 88, 28); [self bringSubviewToFront:_mode];
    {   // 设置入口放安全区左上角；折叠屏内屏圆角比安全区报的大、又有开孔，四周再让 20 点（短边 ≥ 500 点才算大屏）
        CGRect g = self.safeAreaLayoutGuide.layoutFrame; CGFloat inset = MIN(self.bounds.size.width, self.bounds.size.height) >= 500 ? 20 : 4;
        _gear.frame = CGRectMake(CGRectGetMinX(g) + inset, CGRectGetMinY(g) + inset, 36, 36); _gear.hidden = _editing; [self bringSubviewToFront:_gear]; }
    if (!CGRectEqualToRect(_safeRect, safe) || !CGRectEqualToRect(_lastBounds, self.bounds)) {
        if (_editing) [self endEditing];   // 转屏 / 安全区变了：拖到一半的位置按旧几何算，放弃
        [self clearTouches]; _safeRect = safe; _lastBounds = self.bounds;
        if (_usingDefault) { _layout = [S2AVPadLayout defaultForSafeRect:safe]; [self rebuildKeys]; }
        [self wake];
    }
    [self layoutEditBar];
    if (_editing) return;   // 编辑中按键位置由手指决定，不按布局重摆
    CGFloat unit = MIN(safe.size.width, safe.size.height);
    for (NSUInteger i = 0; i < _keys.count; i++) {
        NSDictionary *button = _layout.buttons[i]; UILabel *key = _keys[i];
        key.frame = [_layout frameForButton:button safeRect:safe];
        key.layer.cornerRadius = MIN([button[@"cornerRadius"] doubleValue] * unit, MIN(key.bounds.size.width, key.bounds.size.height) / 2);
    }
    // 截图测试开关（按下态不经 XCTest 也能在真机截到）：S2A_VPAD_TEST_PRESSED=A,X,ZR,UP… 把这些键画成按下态，下一次真触屏即恢复
    const char *testPressed = getenv("S2A_VPAD_TEST_PRESSED");
    if (testPressed && *testPressed) { NSArray *ids = [@(testPressed) componentsSeparatedByString:@","];
        for (UILabel *key in _keys) if ([ids containsObject:key.accessibilityLabel]) vp_fill(key, YES); }
    for (NSDictionary *stick in _layout.sticks) {
        int side = [stick[@"id"] isEqualToString:@"left"] ? 0 : 1;
        _activation[side] = [_layout activationForStick:stick safeRect:safe]; _radius[side] = [_layout radiusForStick:stick safeRect:safe];
        _ring[side].bounds = CGRectMake(0, 0, 2 * _radius[side] + 4, 2 * _radius[side] + 4); _ring[side].layer.cornerRadius = _radius[side] + 2;
        CGFloat knob = _radius[side] * 52 / 60; _knob[side].bounds = CGRectMake(0, 0, knob, knob); _knob[side].layer.cornerRadius = knob / 2;
    }
}
// 与 layoutSubviews 摆键同一份几何（frameForButton + _safeRect），取键逻辑在 vpad_layout.m buttonAt:（单测 t_vpad_stick_press）
- (int)keyAt:(CGPoint)p {
    if (!_keys.count) return -1;
    NSString *identifier = [_layout buttonAt:p safeRect:_safeRect slop:kHit];
    return identifier ? (int)[button_ids() indexOfObject:identifier] : -1;
}
// 手指不动时没有触屏事件：到期时间变了才重排定时器，推摇杆的连续事件不反复建
- (void)armHoldLock {
    double due = S2AVPadHoldLockDeadline(&_holdLock);
    if (due == _holdLockDue) return;
    [_holdLockTimer invalidate]; _holdLockTimer = nil; _holdLockDue = due;
    if (due <= 0) return;
    __weak S2AVPadView *weakSelf = self;
    _holdLockTimer = [NSTimer scheduledTimerWithTimeInterval:MAX(0, due - CACurrentMediaTime()) repeats:NO block:^(NSTimer *t) { (void)t;
        S2AVPadView *view = weakSelf; if (!view) return;
        view->_holdLockTimer = nil; view->_holdLockDue = 0; [view publish]; }];
}
- (void)publish {
    if (self.hidden || !_inputEnabled || _settingsOpen || _editing) return;
    uint32_t held = 0; for (NSNumber *n in _held.objectEnumerator) if (n.intValue >= 0) held |= 1u << n.intValue;
    uint32_t lockChanged = S2AVPadHoldLockStep(&_holdLock, held, kHoldLockKeys, CACurrentMediaTime(), _layoutStore.holdLockEnabled && !_touchOnly);
    uint32_t b = _stickPress | held | _holdLock.locked;
    int32_t axes[4]; for (int i = 0; i < 4; i++) axes[i] = _ax[i];
    s2a_pad_update(S2A_PAD_TOUCH, 1, b, axes);
    for (UILabel *l in _keys) { vp_fill(l, b >> l.tag & 1); vp_lock(l, _layoutStore.holdLockEnabled, _holdLock.locked >> l.tag & 1); }
    // pad_update 已释放输入锁；UIKit 反馈只在主线程触屏事件上运行，不进入 HID / 游戏震动链。
    uint8_t active = (_stick[0] ? 1 : 0) | (_stick[1] ? 2 : 0);
    uint8_t edges = (_stickTravel[0] >= 1 ? 1 : 0) | (_stickTravel[1] >= 1 ? 2 : 0);
    BOOL pulse = S2AVPadHapticStep(&_hapticState, b, active, edges, _layoutStore.hapticFeedbackEnabled && !_touchOnly);
    if (pulse || (lockChanged && _layoutStore.hapticFeedbackEnabled)) {
        if (!_impactFeedback) _impactFeedback = [[UIImpactFeedbackGenerator alloc] initWithStyle:UIImpactFeedbackStyleLight];
        [_impactFeedback impactOccurred]; [_impactFeedback prepare];
    }
    if (lockChanged) [self wake];
    [self armHoldLock];
}
// 碰屏恢复按键高亮；闲置 3 秒后降至玩家设定的最低透明度（缺省 25%）。
- (void)wake {
    if (self.hidden || _touchOnly || _editing) return;   // 纯触屏没有要显示 / 淡出的按键；编辑中按键常亮
    [_fade invalidate]; for (UILabel *l in _keys) {
        [l.layer removeAnimationForKey:@"opacity"];
        l.alpha = _holdLock.locked >> l.tag & 1 ? 1 : S2AVPadKeyOpacity(_layoutStore.idleKeyOpacity, YES) * vp_opacity();   // 锁定的键不淡出，闭锁标记始终看得清
    }
    _fade = [NSTimer scheduledTimerWithTimeInterval:3 repeats:NO block:^(NSTimer *t) { (void)t;
        if (self->_held.count) { [self wake]; return; }
        [UIView animateWithDuration:0.4 animations:^{ for (UILabel *l in self->_keys) if (!(self->_holdLock.locked >> l.tag & 1)) l.alpha = S2AVPadKeyOpacity(self->_layoutStore.idleKeyOpacity, NO) * vp_opacity(); }]; }];
}
- (void)setInputEnabled:(BOOL)enabled {
    BOOL hide = !enabled && !_touchOnly;   // 纯触屏模式下接了实体手柄也留着这一层：手指和手柄是两条独立输入
    if (_inputEnabled == enabled && self.hidden == hide) return;
    _inputEnabled = enabled; [self clearTouches];
    for (UILabel *key in _keys) [key.layer removeAllAnimations];
    self.userInteractionEnabled = enabled || _touchOnly; self.hidden = hide; self.accessibilityElementsHidden = hide;
    if (enabled) { [self publish]; [self wake]; }
    LOG("触屏手柄：%s", enabled ? "恢复" : "隐藏并清空触点");
}
// 游戏画面在屏幕上的矩形：与上屏层（graphics/metal/gal_present.m:183 的 dst_rect）同一套算式——
//   呈现参数取渲染层这一帧的真值，同一套 fminf、同一套「截断后差不到一像素就铺满」（真机 2868×1320 出图
//   1564×720 时 W·rx = 2867.35，不补这一条右侧留一列黑）。两边算出同一个矩形，触点才不会差半格。
// 算式放在「像素」域里做（bounds × nativeScale，与 gal_ios_layer.m 给图层设的 drawableSize 同一份）：
//   铺满阈值是「1 像素」，在点域里写 1.0 就是 1 点 = 3 像素，3x 屏上会比上屏层更容易判满屏（iPhone 16/15
//   2556×1179 出图 1560×720：上屏 2554 宽两侧各 1 px，点域算成整宽）——差 2 px，换设备不必赌这一下。
// 顺手记下这一帧的真实出图尺寸（g_src_w/g_src_h）：触点坐标空间就是它，见 switchPoint。
- (CGRect)gameRect {
    CGRect b = self.bounds;
    if (b.size.width <= 0 || b.size.height <= 0) return CGRectZero;
    int32_t iw = 0, ih = 0; float ax = 0, ay = 0; int32_t st = 0;
    BOOL have = nxgal_present_frame && nxgal_present_frame(&iw, &ih, &ax, &ay, &st);
    if (have) { g_src_w = iw; g_src_h = ih; }
    // 直连模式渲染层不给出图（nxgal_present_frame 只有通用出图层有）：直连上屏的画面比例 = 出图层当前的出图比例（host_adapt.m g_out_a：放宽 2346:1080 铺满、
    //   窄屏加高 1920:屏高、宽屏没生效 / 玩家选回原生 16:9 后 16:9 居中带黑边）。原先一律按屏幕宽 : 1080，后两种情况落点偏（审计 P1-79）；还没挂层时照旧按屏幕宽 : 1080
    else if (s2a_direct_out_aspect && s2a_direct_out_aspect() > 0) { ax = (float)s2a_direct_out_aspect(); ay = 1; st = 0; have = YES; }
    //   装载前（多款容器停在游戏库、单款加载页）不问屏幕宽：本款宽不宽要装载后才判得了，装载前按拉伸铺满（这时还没有游戏画面）
    else { int sw = s2a_screen_w_after_load ? s2a_screen_w_after_load() : 0; if (sw > 0) { ax = (float)sw; ay = 1080; st = 0; have = YES; } }
    BOOL stretched = !have || st || !(ax > 0) || !(ay > 0);   // 还没出过图 / 比例缺失：按满屏
    CGFloat s = self.window.screen.nativeScale ?: UIScreen.mainScreen.nativeScale;
    if (s <= 0) s = 1;
    CGFloat W = MAX(1, round(b.size.width * s)), H = MAX(1, round(b.size.height * s));   // 与 drawableSize 同值
    CGFloat rx = stretched ? 1.0 : MIN(1.0, H * ax / (W * ay));
    CGFloat ry = stretched ? 1.0 : MIN(1.0, W * ay / (H * ax));
    CGFloat dw = W * rx, dh = H * ry;
    if (W - dw < 1.0) dw = W;
    if (H - dh < 1.0) dh = H;
    dw = MAX(trunc(dw), 1), dh = MAX(trunc(dh), 1);   // 上屏层是 int 截断
    CGFloat ox = trunc((W - dw) / 2), oy = trunc((H - dh) / 2);                        // 整数除 2 居中
    CGRect r = CGRectMake(b.origin.x + ox / s, b.origin.y + oy / s, dw / s, dh / s);   // 换回点给 UIKit
    { static CGRect last; static int lw, lh;   // 画面矩形或出图尺寸变了才记一行（查触摸对不上用，不刷屏）
      if (!CGRectEqualToRect(r, last) || lw != g_src_w || lh != g_src_h) {
        last = r; lw = g_src_w; lh = g_src_h;
        LOG("触屏：游戏画面 %.0fx%.0f@%.0f,%.0f 像素（画布 %.0fx%.0f，出图 %dx%d，%s）", dw, dh, ox, oy,
            W, H, g_src_w, g_src_h, stretched ? "拉伸铺满" : "按出图比"); } }
    return r;
}
// 触屏真值一行（g_hud_on 打开时）：手指点 / 发出的触点 / 画面矩形 / 出图尺寸全在一行里。
// 只读不拦触摸；对不上时截一张图就能算出该改哪个数，不必让玩家做标定。
- (void)refreshTouchHud {
    if (!g_hud_on) { [_hud removeFromSuperview]; _hud = nil; }
}
- (void)hudLine:(NSString *)t {
    if (!g_hud_on) { return; }
    if (!_hud) {
        _hud = [[UILabel alloc] init];
        _hud.font = [UIFont monospacedSystemFontOfSize:11 weight:UIFontWeightMedium];
        _hud.textColor = UIColor.whiteColor; _hud.backgroundColor = [UIColor colorWithWhite:0 alpha:0.6];
        _hud.numberOfLines = 3; _hud.textAlignment = NSTextAlignmentCenter;
        _hud.layer.cornerRadius = 4; _hud.clipsToBounds = YES; _hud.userInteractionEnabled = NO;
        [self addSubview:_hud];
    }
    _hud.text = t;
    CGSize sz = [_hud sizeThatFits:CGSizeMake(MAX(self.bounds.size.width - 24, 120), 1e9)];
    _hud.bounds = CGRectMake(0, 0, sz.width + 14, sz.height + 8);
    _hud.center = CGPointMake(CGRectGetMidX(self.bounds), self.bounds.size.height - sz.height / 2 - 16);
}
// 视图点 → 游戏像素。落在画面外返回 (-1,-1)：那里游戏自己的触屏也收不到。
// 分母 = 游戏这一帧真实出图尺寸（g_src_w/g_src_h，gameRect 从渲染层取的那组，除以渲染倍率）：
//   游戏把触点归一化时用的就是自己的画布尺寸，所以「画面内比例 × 画布尺寸」是恒等式，与它按哪个分辨率出图、
//   屏幕是 16:9 还是 19.5:9 还是 4:3、渲染倍率设多少都无关（倍率只乘宿主侧纹理，见 g_res_scale）。
//   不再用「显示宽 1564」：那只在游戏真按显示分辨率出图时成立；出图还是 1280 时按 1564 发会整体放大 22%。
- (CGPoint)switchPoint:(CGPoint)p {
    CGRect g = [self gameRect];
    if (p.x < CGRectGetMinX(g) || p.x > CGRectGetMaxX(g) || p.y < CGRectGetMinY(g) || p.y > CGRectGetMaxY(g)) return CGPointMake(-1, -1);
    int bw = g_src_w, bh = g_src_h;
    // 游戏不知道宽屏（第二层没开：宿主第三层整套放宽或根本没放宽）：游戏按 Switch 触摸屏固定的 1280×720 读触点，宿主把它 16:9 的界面画布
    //   横向拉满整个画面，所以「画面内比例 × 1280」就是游戏坐标（11-02 测试设备2 牧场物语：直连模式渲染层不给出图尺寸，退回显示宽 1564，
    //   手指在画面 68% 处发成 1069、游戏读成 1069/1280 = 84% 处，纯触屏模式一下都点不中；维护者 照片里底部图例贴在屏宽约 93% 处，即 1280 拉满全宽）。
    //   游戏自己出宽图（第二层，s2a_widen_game）时游戏的画布本来就宽，照旧按真实出图 / 显示宽（11-01 银河 2 宽屏局按 1280 点 OK 落到左边即这一类）
    if (!(s2a_widen_game && s2a_widen_game())) { bw = 1280; bh = 720; }
    else if (bw > 0 && bh > 0) { float k = g_res_scale(); if (k != 1.0f) { bw = (int)lroundf(bw / k); bh = (int)lroundf(bh / k); } }
    else if (s2a_game_canvas) s2a_game_canvas(&bw, &bh);   // 渲染层没给（直连模式 / 还没出第一帧）：按游戏画布（宽、高都按屏比，审计 P1-92）   // 渲染层没给（直连模式 / 还没出第一帧）：退回显示宽
    CGFloat x = (p.x - CGRectGetMinX(g)) / MAX(g.size.width, 1) * bw;
    CGFloat y = (p.y - CGRectGetMinY(g)) / MAX(g.size.height, 1) * bh;
    return CGPointMake(MIN(bw - 1, MAX(0, x)), MIN(bh - 1, MAX(0, y)));
}
// HID 线程每帧调一次。返回触点个数（≤ 16）；attributes 用 TouchAttribute（1 = Start、2 = End、0 = 无）。
// 只有「触屏直通」方案才写触点：虚拟按键方案（含默认布局）整层吃掉触摸，游戏收不到任何触点——
//   否则手指去按 A / 摇杆时会被当成点按，游戏里的按钮跟着误触发。
- (int)readTouches:(int32_t *)xy attributes:(uint32_t *)attributes {
    @synchronized(self) {
        // 不碰 UIKit（hidden 之类）——这是 HID 线程。暂停期间不留半截触点，免得恢复后补发一帧 End。
        if (_settingsOpen || !_touchOnly) { for (int i = 0; i < 16; i++) _slotState[i] = 0; return 0; }
        int n = 0;
        for (int i = 0; i < 16; i++) {
            if (_slotState[i] == 0) continue;
            xy[2 * n] = _slotX[i]; xy[2 * n + 1] = _slotY[i];
            if (_slotState[i] == 2) { attributes[n] = 2u; _slotState[i] = 0; }
            else { attributes[n] = _slotStart[i] ? 1u : 0u; _slotStart[i] = NO; }
            n++;
        }
        return n;
    }
}
// 纯触屏：一根手指占一个固定槽位。落在黑边上的按下不建槽（游戏那边同样收不到），后续 move / end 自然忽略。
- (void)beginSlot:(UITouch *)t at:(CGPoint)p {
    CGPoint s = [self switchPoint:p];
    if (_hud || g_hud_on) {
        CGRect g = [self gameRect];
        float k = g_res_scale();
        [self hudLine:[NSString stringWithFormat:@"出图 %dx%d（%@）×%g  画面 %.0fx%.0f@%.0f,%.0f\n手指 %.0f,%.0f → 触点 %.0f,%.0f\n画布 %.0fx%.0f",
                  g_src_w, g_src_h, k == 1.0f ? @"= 触点空间" : @"÷ 倍率 = 触点空间", k, g.size.width, g.size.height, g.origin.x, g.origin.y, p.x, p.y, s.x, s.y,
                  self.bounds.size.width, self.bounds.size.height]];
    }
    { static int n; if (n++ < 8) {   // 前 8 次按下各记一行：真机上触点对不上时有据可查（不刷屏）
        if (s.x < 0) { CGRect g = [self gameRect]; LOG("触屏：手指 %.0f,%.0f 在画面 %.0fx%.0f@%.0f,%.0f 外 → 丢弃", p.x, p.y, g.size.width, g.size.height, g.origin.x, g.origin.y); }
        else LOG("触屏：手指 %.0f,%.0f → 触点 %.0f,%.0f（出图 %dx%d × %g）", p.x, p.y, s.x, s.y, g_src_w, g_src_h, g_res_scale()); } }
    if (s.x < 0) return;
    int slot = -1;
    for (int i = 0; i < 16 && slot < 0; i++) if (_slotState[i] == 0) slot = i;
    for (int i = 0; i < 16 && slot < 0; i++) if (_slotState[i] == 2) slot = i;   // 槽满：挤掉一个已经抬手、还没发完 End 的
    if (slot < 0) return;
    @synchronized(self) {
        _slotX[slot] = (int32_t)lround(s.x); _slotY[slot] = (int32_t)lround(s.y);
        _slotState[slot] = 1; _slotStart[slot] = YES;
    }
    [_slot setObject:@(slot) forKey:t];
    [self publish];
}
- (void)moveSlot:(UITouch *)t at:(CGPoint)p {
    NSNumber *n = [_slot objectForKey:t]; if (!n) return;
    int slot = n.intValue; CGPoint s = [self switchPoint:p]; if (s.x < 0) return;   // 拖到黑边上：停在最后一点
    @synchronized(self) { if (_slotState[slot] == 1) { _slotX[slot] = (int32_t)lround(s.x); _slotY[slot] = (int32_t)lround(s.y); } }
}
- (void)endSlot:(UITouch *)t {
    NSNumber *n = [_slot objectForKey:t]; if (!n) return; [_slot removeObjectForKey:t];
    int slot = n.intValue;
    @synchronized(self) { _slotState[slot] = _slotState[slot] == 1 ? 2 : 0; }   // End 由 HID 线程下一帧发出去
}
- (void)moveStick:(int)k to:(CGPoint)p {
    CGFloat radius = MAX(_radius[k], 1);
    CGFloat dx = (p.x - _c[k].x) / radius, dy = (p.y - _c[k].y) / radius, m = hypot(dx, dy); if (m > 1) { dx /= m; dy /= m; }
    _stickTravel[k] = m;
    _maxMove[k] = MAX(_maxMove[k], hypot(p.x - _c[k].x, p.y - _c[k].y));
    // 触屏源直接写 pad_update，不经过实体手柄的 s2a_pad_stick；死区只应用一次。
    CGPoint output = S2AVPadStickResponse(CGPointMake(dx, dy), _layoutStore.stickDeadzone, _layoutStore.stickSaturation, _layoutStore.stickExponent);
    _ax[2 * k] = (int16_t)lround(output.x * 32767); _ax[2 * k + 1] = (int16_t)lround(-output.y * 32767);
    _knob[k].center = CGPointMake(_c[k].x + dx * radius, _c[k].y + dy * radius);
}
- (void)touchesBegan:(NSSet<UITouch *> *)ts withEvent:(UIEvent *)e {
    if (_editing) {
        for (UITouch *t in ts) {
            CGPoint p = [t locationInView:self]; int k = [self keyAt:p]; if (k < 0) continue;
            for (UILabel *l in _keys) if (l.tag == k) { [_drag setObject:l forKey:t];
                [_dragOffset setObject:[NSValue valueWithCGPoint:CGPointMake(l.center.x - p.x, l.center.y - p.y)] forKey:t]; vp_fill(l, YES); break; }
        }
        return;
    }
    if (_settingsOpen || (!_inputEnabled && !_touchOnly)) return;
    [self wake];
    for (UITouch *t in ts) {
        CGPoint p = [t locationInView:self];
        if (_touchOnly) { [self beginSlot:t at:p]; continue; }   // 纯触屏：没有按键也没有摇杆，这一下直接进游戏
        int k = [self keyAt:p];
        if (k >= 0) { [_held setObject:@(k) forKey:t]; continue; }
        int side = CGRectContainsPoint(_activation[0], p) ? 0 : CGRectContainsPoint(_activation[1], p) ? 1 : -1;
        if (side < 0 || _stick[side]) continue;
        _stick[side] = t; _c[side] = p; _downAt[side] = t.timestamp; _maxMove[side] = 0; [_held setObject:@(-1 - side) forKey:t];
        if (t.timestamp - _lastUp[side] < 0.3 && hypot(p.x - _lastUpAt[side].x, p.y - _lastUpAt[side].y) < 40) _stickPress |= 1u << (side ? B_RS : B_LS);   // 双击：第二下按住 = 摇杆按下
        _ring[side].center = p; _knob[side].center = p; _ring[side].hidden = _knob[side].hidden = NO;
        [self moveStick:side to:p];
    }
    [self publish];
}
- (void)touchesMoved:(NSSet<UITouch *> *)ts withEvent:(UIEvent *)e {
    if (_editing) {
        for (UITouch *t in ts) { UILabel *l = [_drag objectForKey:t]; if (!l) continue;
            CGPoint p = [t locationInView:self], o = [_dragOffset objectForKey:t].CGPointValue;
            CGFloat hw = l.bounds.size.width / 2, hh = l.bounds.size.height / 2;   // 中心钳在安全区内：整个键不出界
            l.center = CGPointMake(MAX(CGRectGetMinX(_safeRect) + hw, MIN(CGRectGetMaxX(_safeRect) - hw, p.x + o.x)),
                                   MAX(CGRectGetMinY(_safeRect) + hh, MIN(CGRectGetMaxY(_safeRect) - hh, p.y + o.y))); }
        return;
    }
    for (UITouch *t in ts) {
        CGPoint p = [t locationInView:self];
        if (_touchOnly) { [self moveSlot:t at:p]; continue; }
        NSNumber *n = [_held objectForKey:t]; if (!n) continue;
        if (n.intValue < 0) { [self moveStick:-1 - n.intValue to:p]; continue; }
        if (n.intValue >= B_LEFT) { int k = [self keyAt:p]; if (k >= B_LEFT) [_held setObject:@(k) forKey:t]; }   // 十字键可以滑着换方向
    }
    [self publish];
}
- (void)end:(NSSet<UITouch *> *)ts {
    uint32_t tap = 0;
    for (UITouch *t in ts) {
        if (_touchOnly) { [self endSlot:t]; continue; }
        NSNumber *n = [_held objectForKey:t]; if (!n) continue; [_held removeObjectForKey:t];
        if (n.intValue < 0) { int side = -1 - n.intValue; uint32_t bit = 1u << (side ? B_RS : B_LS);
            // 点按不拖 = 按一下摇杆（双击第二下按住时已经按着，不再补一下）
            if (!(_stickPress & bit) && S2AVPadStickTapIsPress(t.timestamp - _downAt[side], _maxMove[side])) tap |= bit;
            _stick[side] = nil; _ax[2 * side] = _ax[2 * side + 1] = 0; _stickPress &= ~bit;
            _ring[side].hidden = _knob[side].hidden = YES; _lastUp[side] = t.timestamp; _lastUpAt[side] = [t locationInView:self]; }
    }
    // 先发一拍「按着」再发松开：pad.c 的短按锁存把这次按下留到 HID 下一次读取，和按键短按同一条路
    if (tap) { _stickPress |= tap; [self publish]; _stickPress &= ~tap; }
    [self publish]; [self wake];
}
- (void)editRelease:(NSSet<UITouch *> *)ts { for (UITouch *t in ts) { vp_fill([_drag objectForKey:t], NO); [_drag removeObjectForKey:t]; [_dragOffset removeObjectForKey:t]; } }
- (void)touchesEnded:(NSSet<UITouch *> *)ts withEvent:(UIEvent *)e { if (_editing) { [self editRelease:ts]; return; } [self end:ts]; }
- (void)touchesCancelled:(NSSet<UITouch *> *)ts withEvent:(UIEvent *)e { if (_editing) { [self editRelease:ts]; return; } [self clearTouches]; [self wake]; }
@end

// C# InputPump 按符号查这个导出：每帧问一次当前触点。
// 返回触点个数（0..16）；xy = {x0,y0,…} 用游戏自己的画布坐标系（一般是 1280×720；宽屏款是游戏真出图的宽，如 1564×720），
// attributes 是 TouchAttribute（1 Start、2 End、0 无）。
__attribute__((visibility("default"), used)) int s2a_vpad_read_touch(int32_t *xy, uint32_t *attributes) {
    S2AVPadView *v = g_vpad; return v ? [v readTouches:xy attributes:attributes] : 0;
}
// C# InputPump 按符号查（审计 P1-3）：游戏启用了触屏后 HID 线程每拍调一次，返回 1 = 收下了（之后不再调），0 = 还没出首帧、下一拍再问
//   （首帧前加载页盖在提示条上面，提示了也看不见）。收下后派回主线程按当前方案决定提不提示
__attribute__((visibility("default"), used)) int s2a_vpad_touch_wanted(void) {
    if (!s2a_ap_frames()) return 0;
    dispatch_async(dispatch_get_main_queue(), ^{ [g_vpad offerTouchHint]; });
    return 1;
}

// 切「仅触屏 / 按键」方案（屏角切换钮、提示条、一次成功线「游戏声明触屏配置时自动切」共用一处）：on 1 = 切到仅触屏、0 = 切回按键；
//   manual 1 = 玩家手动（持久化 + 记「手动选过」），0 = 自动（只改本次运行；玩家手动选过时返回 0 不切）；why 进日志。
//   主线程调：同步执行，返回 1 真切了、0 没切、-1 写盘失败或虚拟手柄没挂。别的线程（如 C# HID 线程）调：派主线程执行、立即返回 2
//   （不 dispatch_sync：主线程可能正等 HID 线程的锁）。结果看 s2a_vpad_touch_only() 与日志「触屏布局：切到…」
__attribute__((visibility("default"), used)) int s2a_vpad_set_touch_only(int on, int manual, const char *why) {
    NSString *w = why ? @(why) : @"未注明";
    if (!NSThread.isMainThread) { dispatch_async(dispatch_get_main_queue(), ^{ S2AVPadView *v = g_vpad; if (v) [v setTouchOnly:on != 0 manual:manual != 0 why:w]; }); return 2; }
    S2AVPadView *v = g_vpad; return v ? [v setTouchOnly:on != 0 manual:manual != 0 why:w] : -1;
}
// 当前是不是「仅触屏」方案（含自动切的本次覆盖）：1 是、0 否、-1 虚拟手柄没挂（任意线程，读原子量）
__attribute__((visibility("default"), used)) int s2a_vpad_touch_only(void) { return atomic_load(&g_touch_only); }
// 玩家手动选过方案：1 是、0 否、-1 虚拟手柄没挂（任意线程）
__attribute__((visibility("default"), used)) int s2a_vpad_touch_user_chose(void) { return atomic_load(&g_user_chose); }

void s2a_vpad_attach(UIView *root) {
    S2AVPadView *v = [[S2AVPadView alloc] initWithFrame:root.bounds];
    g_vpad = v;
    v.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight; [root addSubview:v];
    s2a_controller_attach(root, ^(BOOL enabled) { [v setInputEnabled:enabled]; });
    { int cw0 = 1280, ch0 = 720; if (s2a_game_canvas) s2a_game_canvas(&cw0, &ch0);
    LOG("虚拟手柄：已挂到根视图（布局方案：完整 Switch 按键 + 左右浮动摇杆；触点空间取渲染层出图尺寸，未出图时按游戏画布 %d×%d）", cw0, ch0); }
}

void s2a_vpad_add_layout_action(UIAlertController *menu, UIViewController *controller) {
    [g_vpad addLayoutAction:menu controller:controller];
}
void s2a_vpad_set_settings_open(BOOL open) { [g_vpad setSettingsOpen:open]; }
void s2a_vpad_set_touch_hud(BOOL enabled) {
    g_hud_on = enabled;
    [NSUserDefaults.standardUserDefaults setBool:enabled forKey:kHudKey];
    [g_vpad refreshTouchHud];
    LOG("触屏：诊断信息 %s", enabled ? "开" : "关");
}
BOOL s2a_vpad_has_layouts(void) { return g_vpad.window != nil; }
NSString *s2a_vpad_layout_name(void) { return [g_vpad layoutName] ?: @"未加载"; }
UIViewController *s2a_vpad_layout_controller(void) { return g_vpad.window ? [g_vpad layoutController] : nil; }
