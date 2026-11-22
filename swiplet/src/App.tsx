import {
  Alert, BottomSheet, Button, Form, Group, Host, LabeledContent, Link, List, Menu, NavigationStack,
  Picker, RNHostView, Section, SwipeActions, Text, TextField, Toggle, Toolbar, ToolbarItem,
  useNativeState, VStack, ZStack,
} from "@expo/ui/swift-ui";
import {
  buttonStyle, disabled, font, foregroundStyle, ignoreSafeArea, keyboardType, labelStyle,
  monospacedDigit, multilineTextAlignment, navigationBarTitleDisplayMode, navigationTitle, pickerStyle, presentationDetents,
  searchable, tag, tint,
} from "@expo/ui/swift-ui/modifiers";
import { searchPlaces } from "@swipt/api/maps";
import { actions, exit, useService } from "@swipt/api/swiplet";
import { AppleMaps } from "expo-maps";
import { addDatabaseChangeListener, openDatabaseSync } from "expo-sqlite";
import { useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";

import { toGPS, toMap } from "./coordinates";
import type extension from "./extension";
import { holds, restorable, type Held } from "./location";
import {
  checkRoute, MAX_POINTS, pairOf, parseCoordinate, parseRoute, Playback, pointOf, Route,
  type Pair, type Point,
} from "./route";
import { createStore, DATABASE, type Draft, type Favorite, type Request } from "./store";

// The map, the pin and the route draft are in Apple Maps coordinates (GCJ-02
// in mainland China); requests, favorites and typed coordinates are WGS-84.
const api = actions<typeof extension>();
const store = createStore(openDatabaseSync(DATABASE, { enableChangeListener: true }));
const CELLULAR = "shortcuts://run-shortcut?name=SimLocationCellular";
// Surge's own URL scheme. Without Surge and its reflect module, Swipt cannot reach the phone.
const SURGE = "surge:///start";
const ZOOM = 15;

type Mode = "point" | "route";
type Sheet = "search" | "favorites" | "route";

const format = ({ latitude, longitude }: Point) => `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;
const meters = (m: number) => (m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`);
const summary = (draft: Draft, total: number | null) =>
  `${draft.points.length} 个点${total === null ? "" : ` · ${meters(total)}`}`;
const secondary = [font({ textStyle: "subheadline" }), foregroundStyle({ type: "hierarchical", style: "secondary" })];

// Actions run one at a time, in order. Swipt shows what reportError gets.
let queue = Promise.resolve();
const run = (task: () => Promise<unknown>) => {
  queue = queue.then(task).then(() => {}, reportError);
};
const attempt = (task: () => void) => {
  try {
    task();
  } catch (error) {
    reportError(error);
  }
};
const clipboard = async () => {
  const text = await api.readClipboard();
  if (!text) throw new Error("剪贴板里没有文本。");
  return text;
};

function playbackOf(request: Request | null) {
  try {
    return request?.plan.mode === "route" ? new Playback(request.plan.route, request.plan.seed) : null;
  } catch {
    return null; // a route the service cannot play either; its error shows instead
  }
}

const routeOf = (draft: Draft) => checkRoute({
  points: draft.points,
  loop: draft.loop,
  speed: Number(draft.speed),
  speedNoise: Number(draft.speedNoise),
  positionNoise: Number(draft.positionNoise),
});

export default function App() {
  const service = useService<typeof extension>();
  const map = useRef<AppleMaps.MapView>(null);
  const [request, setRequest] = useState(store.latest);
  const [held, setHeld] = useState<Held>(undefined);
  const [draft, setDraft] = useState(store.draft);
  const [favorites, setFavorites] = useState(store.favorites);
  const [mode, setMode] = useState<Mode>(request?.plan.mode === "route" ? "route" : "point");
  // WGS-84, so a favorite or typed coordinate is submitted exactly as given.
  const [target, setTarget] = useState(() => (request?.plan.mode === "point" ? request.plan.point : null));
  const [sheet, setSheet] = useState<Sheet>("search");
  const [sheetShown, setSheetShown] = useState(false);
  const [entering, setEntering] = useState(false);
  const [entry, setEntry] = useState("");
  const [now, setNow] = useState(Date.now);

  // The interface computes a route's position the way the service does.
  const playback = useMemo(() => playbackOf(request), [request]);
  const sample = playback && request ? playback.sample((now - request.created) / 1000) : null;
  const moving = !!sample && !sample.completed;
  const [camera] = useState(() => {
    const plan = request?.plan;
    const gps = plan?.mode === "point" ? plan.point
      : sample ? sample.point
      : draft.points.length ? pointOf(draft.points[0]) : null;
    return gps ? { coordinates: toMap(gps), zoom: ZOOM } : undefined;
  });
  const path = useMemo(() => draft.points.map((p) => toMap(pointOf(p))), [draft.points]);
  const total = useMemo(() => {
    try {
      return new Route(draft.points, draft.loop).total;
    } catch {
      return null;
    }
  }, [draft.points, draft.loop]);

  // A request is only what was asked; what is in effect is what Swipt holds.
  // Read again whenever a device call, here, in Shortcuts or in the service, ends.
  useEffect(() => {
    let reads = 0, first = true, settled = store.settled();
    const refresh = () => {
      const read = ++reads;
      api.heldLocation().then((value) => {
        if (read !== reads) return;
        setHeld(value);
        if (first && value && !camera) {
          setTarget((current) => current ?? value);
          map.current?.setCameraPosition({ coordinates: toMap(value), zoom: ZOOM }).catch(reportError);
        }
        first = false;
      }, (error: unknown) => {
        if (read !== reads) return;
        setHeld(undefined);
        reportError(error);
      });
    };
    refresh();
    const database = addDatabaseChangeListener(({ tableName }) => {
      if (tableName === "requests") setRequest(store.latest());
      if (tableName !== "state") return; // which also holds the draft
      const latest = store.settled();
      if (latest !== settled) {
        settled = latest;
        refresh();
      }
    });
    const foreground = AppState.addEventListener("change", (state) => {
      if (state === "active") refresh();
    });
    return () => {
      reads++;
      database.remove();
      foreground.remove();
    };
  }, [camera]);
  useEffect(() => attempt(() => store.saveDraft(draft)), [draft]);
  useEffect(() => {
    if (!camera) map.current?.setCameraPosition().catch(reportError);
  }, [camera]);
  useEffect(() => {
    if (!moving) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [moving]);

  // A map tap, a search result, typed coordinates or a favorite: it moves
  // the pin, or extends the route.
  const choose = (location: Point) => {
    if (mode === "point") setTarget(toGPS(location));
    else addStop(toGPS(location));
  };
  const addStop = (gps: Point) =>
    setDraft((d) => (d.points.length < MAX_POINTS ? { ...d, points: [...d.points, pairOf(gps)] } : d));
  const reveal = (gps: Point) => {
    if (mode === "point") setTarget(gps);
    else addStop(gps);
    map.current?.setCameraPosition({ coordinates: toMap(gps), zoom: ZOOM }).catch(reportError);
  };
  const show = (kind: Sheet) => {
    setSheet(kind);
    setSheetShown(true);
  };
  const restore = () => run(() => api.clear());

  const pin = target && toMap(target);
  const here = !moving && holds(held, target);
  const entered = parseCoordinate(entry);

  let primary: { label: string; press(): void; stop?: boolean; off?: boolean };
  let caption: string;
  if (mode === "point") {
    primary = here
      ? { label: "恢复真实位置", stop: true, press: restore }
      : { label: "模拟此位置", off: !target, press: () => run(() => api.setPoint(target!)) };
    caption = target ? format(target) : "轻点地图选择位置";
  } else {
    primary = moving
      ? {
        label: "停止移动",
        stop: true,
        press() {
          const point = sample!.point;
          setTarget(point);
          run(() => api.setPoint(point));
        },
      }
      : {
        label: "开始路线",
        off: draft.points.length < 2,
        press: () => run(async () => {
          await api.startRoute(routeOf(draft));
          await service.setEnabled(true);
        }),
      };
    if (service.status === "failed" && service.error) caption = service.error;
    else if (moving) {
      caption = service.status === "running"
        ? `${meters(sample!.progress)} / ${meters(sample!.total)} · ${sample!.speed.toFixed(1)} km/h`
        : service.enabled ? service.error ?? "后台服务启动中…" : "后台服务未开启";
    } else {
      caption = draft.points.length ? summary(draft, total) : "轻点地图添加途经点";
      if (sample?.completed) caption = `已到终点 · ${caption}`;
    }
  }

  return (
    <Host style={{ flex: 1 }}>
      <NavigationStack>
        <Toolbar modifiers={[navigationTitle("SimLocation"), navigationBarTitleDisplayMode("inline")]}>
          <Alert title="输入坐标" isPresented={entering} onIsPresentedChange={setEntering}>
            <Alert.Trigger>
              <ZStack>
                <Group modifiers={[ignoreSafeArea()]}>
                  <RNHostView>
                    <AppleMaps.View
                      ref={map}
                      cameraPosition={camera}
                      markers={mode === "point"
                        ? pin ? [{ id: "pin", coordinates: pin, systemImage: "mappin" }] : []
                        : path.length ? [{ id: "start", coordinates: path[0], systemImage: "flag.fill" }] : []}
                      polylines={mode === "route" && path.length > 1
                        ? [{ id: "route", coordinates: draft.loop ? [...path, path[0]] : path, width: 4 }]
                        : []}
                      // The blue dot is what iOS reports, simulated or not.
                      properties={{ isMyLocationEnabled: true, selectionEnabled: false }}
                      uiSettings={{ togglePitchEnabled: false }}
                      onMapClick={(event) => choose(event.coordinates as Point)}
                    />
                  </RNHostView>
                </Group>
                <BottomSheet isPresented={sheetShown} onIsPresentedChange={setSheetShown}>
                  <Group modifiers={[presentationDetents(["medium", "large"])]}>
                    {sheet === "search" ? (
                      <SearchPlaces
                        onChoose={(location) => {
                          setSheetShown(false);
                          reveal(toGPS(location));
                        }}
                      />
                    ) : sheet === "favorites" ? (
                      <Favorites
                        items={favorites}
                        pin={target}
                        onChoose={(item) => {
                          setSheetShown(false);
                          reveal({ latitude: item.lat, longitude: item.lon });
                        }}
                        onChange={() => setFavorites(store.favorites())}
                      />
                    ) : (
                      <RouteSettings
                        draft={draft}
                        total={total}
                        onChange={setDraft}
                        onImport={(points) => {
                          setDraft((d) => ({ ...d, points }));
                          map.current?.setCameraPosition({ coordinates: toMap(pointOf(points[0])), zoom: ZOOM }).catch(reportError);
                        }}
                      />
                    )}
                  </Group>
                </BottomSheet>
              </ZStack>
            </Alert.Trigger>
            <Alert.Actions>
              <TextField
                placeholder="纬度, 经度（WGS-84）"
                onTextChange={setEntry}
                modifiers={[keyboardType("numbers-and-punctuation")]}
              />
              <Button label="取消" role="cancel" onPress={() => {}} />
              <Button label="完成" onPress={() => entered && reveal(entered)} modifiers={[disabled(!entered)]} />
            </Alert.Actions>
          </Alert>
          <Toolbar.Content>
            <ToolbarItem placement="topBarLeading">
              <Button label="返回" systemImage="chevron.left" onPress={exit} />
            </ToolbarItem>
            <ToolbarItem placement="principal">
              <Picker selection={mode} onSelectionChange={setMode} modifiers={[pickerStyle("segmented")]}>
                <Text modifiers={[tag("point")]}>定点</Text>
                <Text modifiers={[tag("route")]}>路线</Text>
              </Picker>
            </ToolbarItem>
            <ToolbarItem placement="topBarTrailing">
              <Menu label="更多" systemImage="ellipsis" modifiers={[labelStyle("iconOnly")]}>
                <Button label="搜索地点" systemImage="magnifyingglass" onPress={() => show("search")} />
                <Button
                  label="输入坐标"
                  systemImage="number"
                  onPress={() => {
                    setEntry("");
                    setEntering(true);
                  }}
                />
                <Button label="收藏夹" systemImage="star" onPress={() => show("favorites")} />
                {mode === "route" ? (
                  <Button
                    label="撤销上一点"
                    systemImage="arrow.uturn.backward"
                    onPress={() => setDraft((d) => ({ ...d, points: d.points.slice(0, -1) }))}
                    modifiers={[disabled(!draft.points.length)]}
                  />
                ) : null}
                {mode === "route" ? (
                  <Button label="路线设置" systemImage="slider.horizontal.3" onPress={() => show("route")} />
                ) : null}
                {restorable(held, moving) ? (
                  <Button label="恢复真实位置" systemImage="location.slash" role="destructive" onPress={restore} />
                ) : null}
                <Link label="启动 Surge" destination={SURGE} />
                <Link label="蜂窝网络重连" destination={CELLULAR} />
              </Menu>
            </ToolbarItem>
            <ToolbarItem placement="bottomBar">
              <Button
                label={primary.label}
                onPress={primary.press}
                modifiers={[buttonStyle("borderedProminent"), disabled(!!primary.off), ...(primary.stop ? [tint("red")] : [])]}
              />
            </ToolbarItem>
            <ToolbarItem placement="bottomBar">
              <Text modifiers={[font({ textStyle: "caption" }), monospacedDigit()]}>{caption}</Text>
            </ToolbarItem>
          </Toolbar.Content>
        </Toolbar>
      </NavigationStack>
    </Host>
  );
}

function SearchPlaces({ onChoose }: { onChoose(location: Point): void }) {
  const query = useNativeState("");
  const [text, setText] = useState("");
  const [places, setPlaces] = useState<Awaited<ReturnType<typeof searchPlaces>>>([]);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      searchPlaces(text, { signal: controller.signal }).then(setPlaces, (error: Error) => {
        if (error.name !== "AbortError") reportError(error);
      });
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [text]);
  return (
    <NavigationStack>
      <List
        modifiers={[
          navigationTitle("搜索地点"),
          navigationBarTitleDisplayMode("inline"),
          searchable(query, { placement: "navigationBarDrawer", onChange: setText }),
        ]}
      >
        {places.map((place, index) => (
          <Button key={index} onPress={() => onChoose(pointOf(place.coordinate))} modifiers={[buttonStyle("plain")]}>
            <VStack alignment="leading">
              <Text>{place.name ?? ""}</Text>
              <Text modifiers={secondary}>{place.address ?? ""}</Text>
            </VStack>
          </Button>
        ))}
      </List>
    </NavigationStack>
  );
}

function Favorites({ items, pin, onChoose, onChange }: {
  items: Favorite[];
  pin: Point | null;
  onChoose(item: Favorite): void;
  onChange(): void;
}) {
  const field = useNativeState("");
  const [name, setName] = useState("");
  const [copied, setCopied] = useState(false);
  return (
    <NavigationStack>
      <List modifiers={[navigationTitle("收藏夹"), navigationBarTitleDisplayMode("inline")]}>
        <Section footer={<Text>{pin ? `图钉：${format(pin)}` : "先在定点模式下轻点地图放一个图钉。"}</Text>}>
          <TextField text={field} placeholder="名称" onTextChange={setName} />
          <Button
            label="收藏图钉位置"
            systemImage="star"
            onPress={() => attempt(() => {
              store.addFavorite(name, pin!);
              field.set("");
              setName("");
              onChange();
            })}
            modifiers={[disabled(!pin || !name.trim())]}
          />
        </Section>
        {items.length ? (
          <Section title={`${items.length} 个地点`}>
            {items.map((item) => (
              <SwipeActions key={item.id}>
                <Button onPress={() => onChoose(item)} modifiers={[buttonStyle("plain")]}>
                  <VStack alignment="leading">
                    <Text>{item.name}</Text>
                    <Text modifiers={[...secondary, monospacedDigit()]}>
                      {format({ latitude: item.lat, longitude: item.lon })}
                    </Text>
                  </VStack>
                </Button>
                <SwipeActions.Actions>
                  <Button
                    label="删除"
                    systemImage="trash"
                    role="destructive"
                    onPress={() => attempt(() => {
                      store.removeFavorite(item.id);
                      onChange();
                    })}
                  />
                </SwipeActions.Actions>
              </SwipeActions>
            ))}
          </Section>
        ) : null}
        <Section footer={<Text>格式与网页控制台的 var/favorites.json 相同；导入按 id 合并。</Text>}>
          <Button
            label="从剪贴板导入"
            systemImage="doc.on.clipboard"
            onPress={() => run(async () => {
              store.importFavorites(await clipboard());
              onChange();
            })}
          />
          <Button
            label={copied ? "已复制" : "复制全部"}
            systemImage="doc.on.doc"
            onPress={() => run(async () => {
              await api.writeClipboard(store.exportFavorites());
              setCopied(true);
            })}
            modifiers={[disabled(!items.length)]}
          />
        </Section>
      </List>
    </NavigationStack>
  );
}

function RouteSettings({ draft, total, onChange, onImport }: {
  draft: Draft;
  total: number | null;
  onChange(update: (draft: Draft) => Draft): void;
  onImport(points: Pair[]): void;
}) {
  const [copied, setCopied] = useState(false);
  const number = (key: "speed" | "speedNoise" | "positionNoise", label: string) => (
    <LabeledContent label={label}>
      <NumberField value={draft[key]} onChange={(value) => onChange((d) => ({ ...d, [key]: value }))} />
    </LabeledContent>
  );
  return (
    <NavigationStack>
      <Form modifiers={[navigationTitle("路线设置"), navigationBarTitleDisplayMode("inline")]}>
        <Section title={draft.points.length ? summary(draft, total) : "还没有途经点"}>
          <Button
            label="从剪贴板导入"
            systemImage="doc.on.clipboard"
            onPress={() => run(async () => onImport(parseRoute(await clipboard())))}
          />
          <Button
            label={copied ? "已复制" : "复制路线 JSON"}
            systemImage="doc.on.doc"
            onPress={() => run(async () => {
              await api.writeClipboard(JSON.stringify({ points: draft.points }));
              setCopied(true);
            })}
            modifiers={[disabled(draft.points.length < 2)]}
          />
          <Button
            label="清空路线"
            systemImage="trash"
            role="destructive"
            onPress={() => onChange((d) => ({ ...d, points: [] }))}
            modifiers={[disabled(!draft.points.length)]}
          />
        </Section>
        <Section
          title="播放"
          footer={<Text>速度波动是 ±百分比，位置偏移是最大米数，都为 0 时沿路线匀速移动。导入支持 JSON 坐标列表和单段 GPX。</Text>}
        >
          {number("speed", "速度 km/h")}
          {number("speedNoise", "速度波动 %")}
          {number("positionNoise", "位置偏移 m")}
          <Toggle label="循环" isOn={draft.loop} onIsOnChange={(loop) => onChange((d) => ({ ...d, loop }))} />
        </Section>
      </Form>
    </NavigationStack>
  );
}

// Starts from `value`; edits flow out through onChange.
function NumberField({ value, onChange }: { value: string; onChange(value: string): void }) {
  const text = useNativeState(value);
  return (
    <TextField
      text={text}
      onTextChange={onChange}
      modifiers={[keyboardType("numbers-and-punctuation"), multilineTextAlignment("trailing")]}
    />
  );
}
