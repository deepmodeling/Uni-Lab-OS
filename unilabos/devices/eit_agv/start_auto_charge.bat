@echo off
title AGV �Զ������
echo ============================================
echo  AGV �Զ������
echo ============================================
echo.
echo  ˵��: ������������AGV����״̬
echo        AGV��CP6���վʱ�Զ�ִ�г����
echo        AGVִ������ʱ�Զ��ȴ����Ժ�����
echo.
echo  ����: --interval-hours  ��CP6��ɼ���ĵȴ�ʱ��(Сʱ), Ĭ��1
echo        --retry-minutes   ����CP6ʱ�����Լ��(����), Ĭ��5
echo.
echo  �� Ctrl+C ֹͣ���
echo ============================================
echo.

:: ���� conda base ����
call conda activate base
if errorlevel 1 (
    echo [����] conda base ��������ʧ��, ����conda�Ƿ��Ѱ�װ����ʼ��
    pause
    exit /b 1
)

:: �л���ģ���Ŀ¼(eit_agv�ĸ�Ŀ¼)
cd /d d:\Uni-Lab-OS\unilabos\devices

:: ��ģ�鷽ʽ����, ȷ����Ե�����������
:: %* ͸�����������в���, ����: start_auto_charge.bat --interval-hours 2
python -m eit_agv.controller.auto_charge_monitor %*

echo.
echo ============================================
echo  ��������˳�
echo ============================================
pause
