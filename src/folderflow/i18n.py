"""UI-only translation; task IDs, paths and raw engine phases stay unchanged."""
import ctypes
import locale
import re
import string
from .strings import EN

LANGUAGE_CHOICES=[('auto','System / 跟随系统'),('en','English'),('zh_CN','简体中文')]


def system_language():
    try:
        function=ctypes.windll.kernel32.GetUserDefaultUILanguage
        function.restype=ctypes.c_ushort
        return 'zh_CN' if function() & 0x3ff == 0x04 else 'en'
    except (AttributeError,OSError):
        name=(locale.getlocale()[0] or '').lower()
        return 'zh_CN' if name.startswith(('zh','chinese')) else 'en'


def resolve_language(preference='auto'):
    return preference if preference in ('en','zh_CN') else system_language()


class Translator:
    def __init__(self,language):
        self.language=language
        self.patterns=[]
        formatter=string.Formatter()
        for source,target in EN.items():
            if '{p' not in source:
                continue
            pieces,fields=[],[]
            for literal,name,spec,conversion in formatter.parse(source):
                pieces.append(re.escape(literal))
                if name is not None:
                    pieces.append('(.+?)')
                    fields.append(name)
            self.patterns.append((re.compile('^'+''.join(pieces)+'$',re.DOTALL),fields,target))
        self.prefixes=sorted((key for key in EN if '{p' not in key and key.endswith((': ', '：', ' '))),key=len,reverse=True)

    def text(self,source,**values):
        target=EN.get(source,source) if self.language=='en' else source
        return target.format(**values) if values else target

    def message(self,message):
        message=str(message)
        if self.language!='en' or not re.search('[\u4e00-\u9fff]',message):
            return message
        if message in EN:
            return EN[message]
        for pattern,fields,target in self.patterns:
            match=pattern.fullmatch(message)
            if match:
                values=dict(zip(fields,match.groups()))
                return ''.join(literal+(values[name] if name else '')
                    for literal,name,spec,conversion in string.Formatter().parse(target))
        for prefix in self.prefixes:
            if message.startswith(prefix):
                return EN[prefix]+message[len(prefix):]
        return message
