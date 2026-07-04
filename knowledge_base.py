#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import logging

logger = logging.getLogger("captioner")
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))

class KnowledgeBase:
    """从指定子目录加载文档文件
    支持格式：.docx, .txt, .md, .pdf
    """

    def __init__(self, dir_name="knowledge_base"):
        self.kb_dir = os.path.join(TOOL_DIR, dir_name)
        self.text = ""
        self.file_count = 0

    @staticmethod
    def _read_txt(filepath):
        """读取纯文本文件（.txt / .md），尝试多种编码"""
        for encoding in ['utf-8', 'gbk', 'gb2312', 'utf-16', 'latin-1']:
            try:
                with open(filepath, 'r', encoding=encoding) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        # 最后用 errors='replace' 兜底
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()

    @staticmethod
    def _read_docx(filepath):
        """读取 .docx 文件"""
        from docx import Document
        doc = Document(filepath)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)

    @staticmethod
    def _read_pdf(filepath):
        """读取 .pdf 文件，优先 PyPDF2，其次 pdfplumber"""
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(filepath)
            pages = [p.extract_text() or "" for p in reader.pages]
            return "\n".join(pages)
        except ImportError:
            pass
        try:
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            return "\n".join(pages)
        except ImportError:
            pass
        raise ImportError("未安装 PDF 读取库 (PyPDF2 或 pdfplumber)")

    def load(self):
        """扫描目录下所有支持的文档文件，提取文本"""
        if not os.path.exists(self.kb_dir):
            os.makedirs(self.kb_dir)
            logger.info(f"已创建知识库目录: {self.kb_dir}")
            return ""

        # 格式 → 读取方法
        readers = {
            '.txt':  self._read_txt,
            '.md':   self._read_txt,
            '.docx': self._read_docx,
            '.pdf':  self._read_pdf,
        }

        all_text = []
        dir_label = os.path.basename(self.kb_dir)
        for filename in sorted(os.listdir(self.kb_dir)):
            if filename.startswith('~') or filename.startswith('.'):
                continue

            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext not in readers:
                continue

            filepath = os.path.join(self.kb_dir, filename)
            try:
                content = readers[ext](filepath).strip()
                if content:
                    all_text.append(f"--- {filename} ---")
                    all_text.append(content)
                    self.file_count += 1
                    logger.info(f"  加载 {dir_label}: {filename} ({len(content)} 字符)")
            except ImportError as e:
                logger.warning(f"  跳过 {filename}: {e}")
            except Exception as e:
                # 只读独占副本降级加载机制
                import tempfile
                import shutil
                try:
                    temp_dir = tempfile.gettempdir()
                    temp_filepath = os.path.join(temp_dir, f"temp_kb_{filename}")
                    shutil.copy2(filepath, temp_filepath)  # 尝试物理强复制
                    content = readers[ext](temp_filepath).strip()
                    try:
                        os.remove(temp_filepath)
                    except Exception:
                        pass
                    if content:
                        all_text.append(f"--- {filename} (只读锁定副本) ---")
                        all_text.append(content)
                        self.file_count += 1
                        logger.info(f"  通过只读临时副本成功加载了正被Word占用的文件 {dir_label}: {filename} ({len(content)} 字符)")
                except Exception as fallback_err:
                    logger.error(f"  读取 {filename} 失败（已尝试只读临时副本降级，依然失败）: {e}", exc_info=True)

        self.text = "\n".join(all_text)
        logger.info(f"{dir_label} 加载完成: {self.file_count} 个文件, {len(self.text)} 字符")
        return self.text
