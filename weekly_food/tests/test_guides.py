import unittest
from datetime import datetime
from unittest.mock import Mock
from weekly_food.core import Evidence, KST, Topic
from weekly_food.guides import validate_guide, make_guide, render_guide, detail_queries


class GuideTests(unittest.TestCase):
    def source(self, text, url='https://example.com/recipe', kind='recipe_source'):
        return Evidence('참고 원문', url, datetime.now(KST).isoformat(), kind, '원문', 'source', summary=text)

    def test_exact_numbers_and_one_base_recipe(self):
        base = self.source('강력분 300g을 준비하고 180도에서 20분 굽습니다.')
        other = self.source('강력분 600g을 준비합니다.', 'https://example.com/other')
        facts = [
            {'field': 'ingredients', 'text': '강력분 300g을 준비해요.', 'source_id': base.id, 'quote': base.summary},
            {'field': 'ingredients', 'text': '강력분 600g을 준비해요.', 'source_id': other.id, 'quote': other.summary},
            {'field': 'baking', 'text': '200도에서 20분 구워요.', 'source_id': base.id, 'quote': base.summary},
        ]
        guide = validate_guide({'facts': facts}, [base, other], 'recipe', base.id)
        self.assertEqual(len(guide['fields']['ingredients']), 1)
        self.assertFalse(guide['fields']['baking'])
        self.assertTrue(guide['issues'])
        no_base = validate_guide({'facts': facts}, [base, other], 'recipe')
        self.assertFalse(no_base['fields']['ingredients'])

    def test_hours_prices_and_parking_are_allowed_when_evidenced(self):
        source = self.source('평일 11:00~20:00 운영합니다. 주차 1시간 무료입니다. 대표 메뉴 12000원입니다.', kind='venue_source')
        facts = [{'field': key, 'text': text, 'source_id': source.id, 'quote': source.summary}
                 for key, text in [('hours', '평일 11:00~20:00 운영해요.'), ('parking', '주차 1시간 무료예요.'), ('menu', '대표 메뉴는 12000원이에요.')]]
        guide = validate_guide({'facts': facts}, [source], 'restaurant')
        self.assertTrue(guide['fields']['hours'])
        self.assertTrue(guide['fields']['parking'])
        self.assertTrue(guide['fields']['menu'])
        self.assertIn('[확인 필요]', render_guide('식당', guide)[1])

    def test_no_title_only_or_fake_quote_and_conflicts(self):
        a = self.source('11:00부터 운영합니다.', kind='venue_source')
        b = self.source('12:00부터 운영합니다.', 'https://example.com/b', 'venue_source')
        rows = [{'field': 'hours', 'text': s.summary, 'source_id': s.id, 'quote': s.summary} for s in [a, b]]
        guide = validate_guide({'facts': rows}, [a, b], 'restaurant')
        self.assertFalse(guide['fields']['hours'])
        self.assertTrue(guide['issues'])
        rows[0]['quote'] = '존재하지 않는 원문'
        self.assertFalse(validate_guide({'facts': rows[:1]}, [a], 'restaurant')['fields']['hours'])

    def test_provided_recipe_works_without_ai_and_has_real_structure(self):
        source = self.source('소개: 테스트용 빵이에요. 분량: 테스트용 6개입니다. 재료: 강력분 300g입니다. 도구: 반죽 볼과 오븐입니다. 과정: 반죽을 섞고 발효 후 성형합니다. 굽기: 180도에서 20분입니다. 팁: 원문의 반죽 상태를 확인합니다. 보관: 원문에 표시된 조건을 따릅니다.')
        topic = Topic('테스트 빵', '디저트', [source.id])
        topic.guide_type, topic.base_source_id = 'recipe', source.id
        guide = make_guide(topic, [source])
        self.assertTrue(guide['complete'], guide['missing'])
        title, text = render_guide(topic.name, guide)
        self.assertIn('만들기', title)
        self.assertIn('## 단계별 만드는 방법', text)
        self.assertIn('300g', text)
        self.assertNotIn('[확인 필요]', text)

    def test_type_specific_research_and_no_imagined_recipe(self):
        self.assertTrue(any('주차' in q for q in detail_queries('식당', 'restaurant')))
        self.assertTrue(any('온도' in q for q in detail_queries('빵', 'recipe')))
        topic = Topic('빵', '디저트', [])
        guide = make_guide(topic, [])
        self.assertFalse(guide['complete'])
        self.assertTrue(guide['missing'])
