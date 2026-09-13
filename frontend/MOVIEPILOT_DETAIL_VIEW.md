# MoviePilot 风格详情页组件

## 概述

为 Emby Controller 的用户端创建了一个完全复刻 MoviePilot 风格的影片详情页面组件。该组件严格按照参考图片（5.png 和 9.png）的视觉设计和布局实现。

## 文件位置

- **组件文件**: `frontend/src/portal/MediaDetailView.vue`
- **集成位置**: `frontend/src/portal/RequestApp.vue`（已更新导入并使用新组件）

## 主要特性

### 1. 视觉设计 (完全复刻 MoviePilot)

#### 背景和氛围
- 大幅模糊背景图（使用 backdrop_url，20px 高斯模糊）
- 深色渐变遮罩（从透明到 #0f1117）
- 固定位置背景，滚动时保持视觉连续性

#### 配色方案
- **主背景**: `#0f1117` (深灰黑)
- **卡片背景**: `rgba(23, 26, 35, 0.8)` 带半透明和背景模糊
- **主按钮**: `#5b8cff` (蓝色)
- **次按钮**: `#22d3ee` (青色)
- **警告按钮**: `#fbbf24` (黄色) 半透明边框
- **成功状态**: `#22c55e` (绿色)
- **文本颜色**: 
  - 主文本 `#e5e7eb`
  - 次要文本 `#9ca3af`
  - 标题 `#fff`

### 2. 布局结构

#### Hero 区域（顶部）
```
[海报] [标题 + 元数据 + 操作按钮] [信息面板]
```

- **海报**: 220px 宽，8px 圆角，带阴影和类型徽章
- **标题区**: 
  - 2rem 标题字体，加粗
  - 年份用灰色显示
  - 元数据行：年份 · 时长 · 类型
  - 操作按钮行：搜索资源、搜索字幕、订阅、在线播放
- **信息面板**: 280px 宽
  - 评分展示（星级 + 数字）
  - ID、原始标题、状态、上映日期等

#### 内容区域
- **简介**: 标题 + 正文
- **Producer**: 制作人列表
- **外部链接**: TheMovieDb、IMDb、TheTvDb（圆角按钮，带🔗图标）
- **季度列表** (电视剧):
  - 每季一个卡片
  - 包含复选框、季度号、集数、状态徽章、收藏按钮、展开按钮
  - 可展开显示季度简介和集列表
- **演员阵容**: 
  - 圆形头像（1:1 比例）
  - 网格布局，每行最多填充
  - 显示演员名和角色

### 3. 交互功能

#### 季度选择（电视剧）
- 复选框选择订阅哪些季
- 默认全选所有季度
- 展开/收起按钮显示详细信息

#### 状态显示
- **已入库**: 绿色 (#22c55e)
- **部分缺失**: 橙色 (#f59e0b)
- **缺失**: 红色 (#ef4444)

#### 按钮响应
- `@close`: 关闭详情页
- `@subscribe`: 提交订阅（包含选中的季度）
- `@search`: 搜索资源
- `@play`: 在线播放

### 4. 响应式设计

#### 桌面端 (>1024px)
- 三列布局：海报 | 主内容 | 信息面板
- 演员网格：每行自动填充（最小 140px）

#### 移动端 (<1024px)
- 单列布局
- 海报居中显示，宽度 180px
- 信息面板占满宽度
- 保持所有功能可用

### 5. 组件 Props

```typescript
interface MediaDetailProps {
  mediaItem: {
    id?: number;
    tmdb_id: number;
    media_source?: string;
    media_id?: string;
    media_type: 'movie' | 'tv';
    title: string;
    original_title?: string | null;
    year?: number | null;
    overview?: string | null;
    poster_url?: string | null;
    backdrop_url?: string | null;
    status?: string | null;
    release_date?: string | null;
    tagline?: string | null;
    rating?: number | null;
    runtime_minutes?: number | null;
    genres?: string[];
    directors?: { id?: number; name: string; job?: string }[];
    producers?: { id?: number; name: string; job?: string }[];
    cast?: { id?: number; name: string; character?: string; profile_url?: string }[];
    seasons?: number | null;
    episodes?: number | null;
    imdb_id?: string | null;
    tvdb_id?: string | number | null;
    library_state?: string;
    moviepilot_subscribe_state?: string;
    season_info?: Array<{...}>;
    episodes_info?: Record<string, Array<{...}>>;
  };
}
```

### 6. 事件 Emits

```typescript
emits: {
  close: [];
  subscribe: [seasons?: number[]];
  search: [];
  play: [];
}
```

## 集成说明

### 在 RequestApp.vue 中使用

```vue
<script setup lang="ts">
import MediaDetailView from './MediaDetailView.vue';
</script>

<template>
  <MediaDetailView
    v-if="detailVisible && selected"
    :media-item="selected"
    @close="closeDetailDialog"
    @subscribe="submitRequest"
    @search="() => {}"
    @play="() => {}"
  />
</template>
```

## CSS 类名约定

所有类名使用 `mp-` 前缀，表示 MoviePilot 风格：

- `.mp-detail-modal` - 根容器
- `.mp-detail-backdrop` - 背景图层
- `.mp-detail-hero` - Hero 区域
- `.mp-detail-poster` - 海报图片
- `.mp-action-btn` - 操作按钮
- `.mp-detail-info-panel` - 信息面板
- `.mp-season-card` - 季度卡片
- `.mp-cast-grid` - 演员网格

## 样式特点

### 圆角和阴影
- 小元素圆角: 4px-6px
- 卡片圆角: 8px
- 按钮圆角: 6px（操作按钮）、20px（外部链接）
- 头像: 50% 完全圆形
- 阴影: `0 10px 30px rgba(0, 0, 0, 0.5)` 用于海报

### 间距规范
- 区域间距: 2rem-3rem
- 元素间距: 0.5rem-1rem
- 内边距: 
  - 按钮 `0.625rem 1.25rem`
  - 卡片 `1rem-1.5rem`

### 字体大小
- 主标题: 2rem (32px)
- 次标题: 1.5rem (24px)
- 章节标题: 1.25rem (20px)
- 正文: 0.95rem-1rem
- 小字: 0.75rem-0.875rem

### 过渡效果
- 所有交互元素: `transition: all 0.2s`
- 按钮悬停态背景色变化
- 平滑的颜色过渡

## 浏览器兼容性

- 现代浏览器（Chrome, Firefox, Safari, Edge 最新版）
- 使用 CSS Grid 和 Flexbox
- 使用 `backdrop-filter: blur()` 实现毛玻璃效果
- 支持触摸设备的滚动和交互

## 未来改进

1. **加载状态**: 添加骨架屏或加载动画
2. **错误处理**: 图片加载失败的占位符
3. **动画效果**: 进入/退出动画
4. **国际化**: 支持多语言切换
5. **无障碍**: 增强键盘导航和屏幕阅读器支持
6. **性能优化**: 图片懒加载和虚拟滚动

## 参考文件

- 设计参考: `/imagetest/5.png`, `/imagetest/9.png`
- MoviePilot 源码: `/方案/Frontend/src/views/discover/MediaDetailView.vue`
