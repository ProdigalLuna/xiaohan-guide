// 语音输入：浏览器原生 Web Speech API + 方言模拟降级
const MOCK_SAMPLES = {
  "": [
    "我想买个拍照好的手机，预算四千左右",
    "小米 15 和 iPhone 16 哪个好",
    "两千五以内，续航强一点的学生机",
  ],
  sichuan: [
    "我要个巴适的，拍照巴适、续航又强的，好多钱嘛",
    "相因点的嘛，三千以内有啥子好推荐",
  ],
  yue: [
    "我想買部影相靚嘅手機，幾多錢左右啊",
    "邊隻抵玩啲？三千蚊以內",
  ],
  dongbei: [
    "整一个嘎嘎好的手机，拍照贼拉好使",
    "三千以内，啥玩意儿最划算",
  ],
  henan: [
    "中不中？两千五以内的手机给我推荐一个",
    "啥价儿嘛，恁说说续航强的哪款",
  ],
  shanghai: [
    "拍照老灵额手机，几钿啊",
    "便宜点伐，三千左右适意的",
  ],
};

export class VoiceInput {
  constructor({ onInterim, onFinal, onState, onError }) {
    this.onInterim = onInterim || (() => {});
    this.onFinal = onFinal || (() => {});
    this.onState = onState || (() => {});
    this.onError = onError || (() => {});
    this.recognition = null;
    this.active = false;
    this.finalText = "";
  }

  get supported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  start(lang = "zh-CN") {
    if (!this.supported) {
      this.onState("unsupported");
      return false;
    }
    this.stop();
    const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new Ctor();
    rec.lang = lang;
    rec.continuous = false;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    this.finalText = "";
    this.active = true;

    rec.onstart = () => this.onState("recording");
    rec.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          this.finalText += transcript;
        } else {
          interim += transcript;
        }
      }
      if (interim) this.onInterim(interim);
    };
    rec.onerror = (event) => {
      this.active = false;
      const map = {
        "not-allowed": "麦克风权限被拒绝，请在浏览器中允许使用麦克风",
        "service-not-allowed": "当前浏览器不允许使用语音识别服务",
        "no-speech": "没有听到声音，请再说一次",
        network: "语音识别网络异常，已切换到模拟输入",
        "audio-capture": "没有检测到麦克风设备",
      };
      this.onError(map[event.error] || `语音识别出错：${event.error}`);
      this.onState("idle");
    };
    rec.onend = () => {
      this.active = false;
      this.onState("idle");
      const text = this.finalText.trim();
      if (text) this.onFinal(text);
    };

    try {
      rec.start();
      this.recognition = rec;
      return true;
    } catch (err) {
      this.active = false;
      this.onError("语音识别启动失败：" + err.message);
      return false;
    }
  }

  stop() {
    if (this.recognition && this.active) {
      try {
        this.recognition.stop();
      } catch (err) {
        /* ignore */
      }
    }
    this.active = false;
  }

  mockSample(dialectCode) {
    const list = MOCK_SAMPLES[dialectCode] || MOCK_SAMPLES[""];
    return list[Math.floor(Math.random() * list.length)];
  }
}

// 语音播报
export function speak(text) {
  if (!("speechSynthesis" in window) || !text) return false;
  try {
    window.speechSynthesis.cancel();
    const clean = text.replace(/（[^）]*）/g, "").replace(/[·•]/g, "").slice(0, 180);
    const utter = new SpeechSynthesisUtterance(clean);
    utter.lang = "zh-CN";
    utter.rate = 1.03;
    window.speechSynthesis.speak(utter);
    return true;
  } catch (err) {
    return false;
  }
}
