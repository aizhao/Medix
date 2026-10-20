"""Deterministic safety/evidence policy for the unified agent workflow."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class QueryRoute:
    route: str
    reason: str
    high_risk: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_SMALL_TALK = re.compile(
    r"^(你好|您好|嗨|hello|hi|谢谢|感谢|再见|早上好|下午好|晚上好|你是谁|你能做什么)[呀啊哦！!。.？?\s]*$",
    re.IGNORECASE,
)
_PRODUCT_OPERATION = re.compile(
    r"(上传|下载|删除|重命名|新建|知识库|切片|解析|向量|会话|登录|页面|按钮|怎么用|使用方法|系统设置|导入文件)",
    re.IGNORECASE,
)
_MEDICAL_TERMS = re.compile(
    r"(症状|疼|痛|发热|发烧|咳|喘|胸闷|头晕|恶心|呕吐|腹泻|出血|皮疹|"
    r"疾病|诊断|治疗|用药|药物|剂量|检查|化验|指标|血压|血糖|肿瘤|癌|感染|"
    r"指南|禁忌|副作用|不良反应|手术|预后|并发症|疫苗|怀孕|妊娠|儿童|婴儿|"
    r"doctor|medical|symptom|disease|diagnos|treat|drug|dose|medicine)",
    re.IGNORECASE,
)
_URGENT_SIGNS = re.compile(
    r"(胸痛.{0,8}(呼吸困难|大汗|濒死)|呼吸困难|无法呼吸|意识不清|昏迷|抽搐不止|"
    r"大出血|止不住血|口眼歪斜|一侧无力|突然说不出话|严重过敏|喉头水肿|"
    r"自杀|轻生|服毒|药物过量)",
    re.IGNORECASE,
)
_HIGH_RISK = re.compile(
    r"(剂量|用量|停药|换药|处方|禁忌|过量|孕妇|怀孕|妊娠|哺乳|儿童|婴儿|"
    r"肝功能|肾功能|诊断|确诊|治疗方案|手术|急救|drug dose|dosage|pregnan)",
    re.IGNORECASE,
)


def classify_query(question: str, context: Optional[Dict[str, Any]] = None) -> QueryRoute:
    """Return policy metadata; the coordinator keeps one execution graph.

    Only urgent requests bypass the Agent/MedRGAG graph.  The
    ``medical_evidence`` value is a policy flag consumed after Worker agents
    contribute, not a separate top-level execution route.
    """
    text = " ".join(str(question or "").strip().split())
    if _URGENT_SIGNS.search(text):
        return QueryRoute("urgent", "检测到需要优先线下急救处置的危险信号", True)
    if _SMALL_TALK.fullmatch(text):
        return QueryRoute("direct", "寒暄或能力询问，不需要医学证据检索")
    if _PRODUCT_OPERATION.search(text) and not _MEDICAL_TERMS.search(text):
        return QueryRoute("direct", "产品操作问题，不需要医学证据检索")
    if _MEDICAL_TERMS.search(text):
        return QueryRoute("medical_evidence", "医学事实或建议问题，强制执行 MedRGAG", bool(_HIGH_RISK.search(text)))
    # MediX is a medical assistant: unknown substantive requests take the safe,
    # evidence-grounded path instead of silently producing an ungrounded answer.
    return QueryRoute("medical_evidence", "非寒暄的实质问题默认进入证据流程", bool(_HIGH_RISK.search(text)))


def urgent_response(question: str) -> Dict[str, Any]:
    """Return immediate safety guidance without waiting for a long RAG chain."""
    return {
        "answer": (
            "## 请立即寻求紧急医疗帮助\n\n"
            "你描述的情况可能包含需要立即处理的危险信号。请马上拨打当地急救电话（中国大陆为 120），"
            "或由身边的人陪同前往最近的急诊。不要自行驾车，也不要等待在线问答结果。\n\n"
            "在等待救援时，保持呼吸道通畅，避免自行服用新的药物；如果患者失去意识且无正常呼吸，"
            "请按急救调度员指导实施心肺复苏。"
        ),
        "mode": "urgent_safety",
        "swarm_enabled": False,
        "agents_involved": ["safety_router"],
        "suggestions": ["立即拨打 120 或前往急诊", "不要自行驾车", "按急救调度员指引处置"],
        "citations": [],
        "evidence_trace": {
            "route": QueryRoute("urgent", "危险信号优先于耗时检索", True).to_dict(),
            "evidence_gate": {"status": "urgent_bypass", "reason": "先急救、后检索"},
        },
        "warnings": ["该回答是危险信号分流提示，不构成诊断。"],
        "disclaimer": "紧急情况下请以急救中心和现场医务人员指导为准。",
    }


__all__ = ["QueryRoute", "classify_query", "urgent_response"]
